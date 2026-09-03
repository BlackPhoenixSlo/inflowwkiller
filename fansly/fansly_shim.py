"""
Fansly-as-OnlyFans shim — the inbox adapter.

The relay's inbox route (`server._assemble_chats_page`) takes a `client` and
calls `client.list_chats(...)` / `client.get_messages(...)`, then reads OF's
response shape (`{"list": [...], "hasMore": bool}`, rows with `withUser` /
`lastMessage` / `unreadMessagesCount`, messages with `fromUser` / `text` /
`createdAt`). It never knows which platform it's talking to.

So the cheapest possible "Fansly inbox" is a client that answers those exact
methods with those exact shapes — translating Fansly's own responses on the way
out. Nothing downstream (route, cache, DB, UI) changes. That is what this is.

`FanslyShimClient` extends `FanslyClient` (real Fansly calls, §fansly_client) and
adds the OF-shaped read surface. The **reshaping is pure module functions**
(`of_chat_row`, `of_message`, `of_user`) so `test_shim_shapes.py` can verify them
against the captured HAR with no network.

Scope: read-only inbox (me, list_chats, get_messages, chat_folders). Writes
(send/typing/follow) already live on `FanslyClient` and are OF-agnostic. Not
every OF field is filled — only the ones the inbox actually reads; unknown OF
fields default to OF's own empty value, never a fabricated one.
"""
from __future__ import annotations

import functools as _functools
import time
import json as _json
import os as _os
import threading
from collections import OrderedDict
from collections.abc import Set as AbstractSet
import logging
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any

import requests

from fansly_client import MEDIA_BASE, FanslyAPIError, FanslyClient
from fansly_vault import VaultMixin

log = logging.getLogger("of-relay.fansly.shim")


# ---------------------------------------------------------------------------
# Capability contract — deliberate no-ops
# ---------------------------------------------------------------------------
# Some OFClient methods describe a feature Fansly simply does not have (message
# pinning, vault deletion, story creation). A plugin calling one of those must
# not AttributeError mid-run — that takes down the whole automation, which is
# strictly worse than the feature being absent. But a bare `return {}` is worse
# still in the other direction: six months on it is indistinguishable from a
# real implementation, so nobody ever finishes it.
#
# This decorator is the middle path. It marks the method as INTENTIONALLY empty,
# carries the reason in the code, logs once per process (not once per call — an
# automation loop would flood the log), and is machine-readable, so
# test_fansly_capability.py can tell "deliberately unsupported" apart from
# "someone forgot". Never delete the marker to "clean up" a method; if Fansly
# grows the feature, replace the whole decorator with the real implementation.
_UNSUPPORTED_LOGGED: set[str] = set()


def fansly_unsupported(reason: str, returns: Any = None):
    """Mark a method as a permanent no-op because Fansly lacks the feature.

    `returns` is the OF-shaped empty value the caller expects — a callable is
    invoked per call so mutable defaults ({} / []) are never shared between
    callers, which would let one plugin mutate another's "empty" result."""
    def decorate(fn):
        @_functools.wraps(fn)
        def wrapper(*_args: Any, **_kwargs: Any) -> Any:
            if fn.__name__ not in _UNSUPPORTED_LOGGED:
                _UNSUPPORTED_LOGGED.add(fn.__name__)
                log.info("fansly: %s is a no-op — %s", fn.__name__, reason)
            return returns() if callable(returns) else returns
        wrapper.fansly_unsupported = reason      # the machine-readable marker
        return wrapper
    return decorate


def fansly_blocked(reason: str, returns: Any = None):
    """Mark a method Fansly PROBABLY supports but that we cannot ship yet.

    Distinct from fansly_unsupported, and the distinction is the point: an
    unsupported method is DONE (Fansly lacks the feature, nothing will change
    it), whereas a blocked one is UNFINISHED — the endpoint likely exists but
    the request body is uncaptured, because capturing it needs a real
    subscriber, a real payment, or an actual mass-blast from this account.

    Collapsing the two would be the expensive mistake in both directions: a
    blocked method left as a bare AttributeError takes down a live automation
    today, while one silently marked "unsupported" would never get finished.

    So these return the caller's empty shape (the automation degrades to a
    clean skip instead of crashing) and log a WARNING, not info — this one is
    meant to be noticed and eventually removed."""
    def decorate(fn):
        @_functools.wraps(fn)
        def wrapper(*_args: Any, **_kwargs: Any) -> Any:
            if fn.__name__ not in _UNSUPPORTED_LOGGED:
                _UNSUPPORTED_LOGGED.add(fn.__name__)
                log.warning("fansly: %s is not wired yet — %s",
                            fn.__name__, reason)
            return returns() if callable(returns) else returns
        wrapper.fansly_blocked = reason
        return wrapper
    return decorate


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
# Fansly quotes every price in a minor unit worth 1/1000 of a dollar. That is
# ANCHORED, not inferred: /orders/products lists the wallet top-ups with the
# dollar amount in the product NAME beside the machine price —
#   "$0.01 Wallet Balance" -> price 10       "$5 Wallet Balance"  -> price 5000
#   "$10 Wallet Balance"   -> price 10000    "$25 Wallet Balance" -> price 25000
# so 1 USD == 1000 units, and the captured PPV send's 101000 is $101.00.
# Guessing cents here would undercharge every PPV by 10x, so do not "simplify"
# this to 100 without re-reading that products response.
FANSLY_PRICE_UNIT = 1000

# Notification codes that mean "a fan paid for media" — the only creator-visible
# purchase signal Fansly exposes (every sales endpoint 404s). 2007/2008 are the
# client's own "Media Purchases" labels; 32007 is locked TEXT, which unlocks the
# same message, so it counts too. See _purchases_by_fan.
_PURCHASE_NOTIF_TYPES = {2007, 2008, 32007}
# How long a fan's purchase set is reused before re-reading the feed. Short: a
# purchase should appear on the next ↻, not after a restart.
_PURCHASE_TTL_S = 60
# Where a purchase notification carries its price, in precedence order.
# `accountMediaPrice` is the one OBSERVED live (2026-09-02); the others are
# defensive. Shared with service/fansly_revenue so the dollar figure this shim
# renders into the row text and the amount that lane banks in the ledger can
# never be read from different fields.
_PRICE_METADATA_KEYS = ("accountMediaPrice", "price", "amount")


def _fansly_price(usd: Any) -> int:
    """OF-side dollars -> Fansly's 1/1000-dollar integer unit."""
    return int(round(float(usd or 0) * FANSLY_PRICE_UNIT))



def vault_ids_of(media_files: Any) -> list[str]:
    """Normalise OF's `mediaFiles` to the vault mediaIds a Fansly send attaches.

    Entries are vault ids (int/str), or dicts. `upload_media` returns the
    vault id directly in `send_with`, but a caller holding the whole upload
    result (`{vault_id, send_with, ...}`) may pass that instead — accept any
    dict that names a vault id. What is still refused is OF's own fresh-upload
    CLAIM (`{processId, host, ...}`): that is an artifact of OF's convert
    pipeline, and no Fansly call can consume it — the bytes have to go through
    `upload_media`, which yields a vault id. Refusing loudly beats attaching
    nothing while the composer reports a media send."""
    out: list[str] = []
    for m in list(media_files or []):
        if not m:
            continue
        if isinstance(m, dict):
            mid = m.get("mediaId") or m.get("vault_id") or m.get("id")
            if not mid:
                raise FanslyAPIError(
                    "Fansly attaches VAULT media. This looks like an OF "
                    "fresh-upload claim (processId/host), which Fansly cannot "
                    "consume — run the bytes through upload_media (POST "
                    "/api/of/v2/upload) and attach the vault id it returns."
                )
            out.append(str(mid))
        else:
            out.append(str(m))
    return out


def check_sendable(*, media_files: Any = None, price: Any = 0,
                   previews: Any = None, locked_text: Any = None) -> None:
    """Raise FanslyAPIError for a send Fansly genuinely cannot express.

    THE SINGLE SOURCE OF TRUTH for these four rules, deliberately module-level
    and free of any client state so a caller with no session (the relay's
    SCHEDULE route) can ask the same question the sender will ask later.

    That mattered immediately: these rules were briefly duplicated by hand in
    server.py's Fansly schedule branch and drifted within a day — the
    all-previews rule was added here and not there, so a scheduled send could
    be accepted and then fail hours later at fire time, which is precisely the
    silent-deferred-failure the schedule-time check exists to prevent. One
    copy, two callers, no drift.

    Every rule below refuses something that would otherwise SUCCEED-looking
    while being wrong about money or privacy; none is mere input validation."""
    vault_ids = vault_ids_of(media_files)
    price_usd = float(price or 0)
    if price_usd <= 0:
        return
    # Fansly prices the MEDIA (through the accountMedia grant), not the
    # message. A text-only "PPV" has nowhere to put the price, and sending it
    # anyway would deliver the tease FREE while the UI reported a paid send.
    if not vault_ids:
        raise FanslyAPIError(
            "A Fansly PPV price rides on the attached media, so a paid "
            "send needs at least one vault attachment."
        )
    # Every attachment free on a PAID send means the fan pays nothing and
    # receives everything, while the composer reports a sale. OF's preview
    # counter can legitimately be dragged up to the full attachment count, so
    # this is reachable by ordinary UI use, not a malformed request.
    wanted = {str(v) for v in vault_ids}
    if len(({str(p) for p in (previews or [])} & wanted)) >= len(wanted):
        raise FanslyAPIError(
            "Every attachment is marked as a free preview, so a paid send "
            "would deliver all of it for nothing. Leave at least one "
            "attachment locked, or send it free."
        )
    # Fansly's message text is always visible. OF's "lock the text behind the
    # price" has no wire field here, so honouring it silently would publish
    # text the sender asked to keep hidden.
    if locked_text:
        raise FanslyAPIError(
            "Fansly always shows the message text — only the media locks. "
            "Send without 'lock text', or move the tease into the media."
        )


# ---------------------------------------------------------------------------
# KLIPY gif URL resolution
# ---------------------------------------------------------------------------
# Fansly sends a GIF as a message whose `content` is the KLIPY *page* url
# (https://klipy.com/gifs/<slug>) — NOT a renderable image. The direct gif
# (static.klipy.com/....gif) only ever appears in the /gif-provider list we
# serve to the picker, so we remember page<->gif there and resolve it back when
# a message carrying that page url flows through `of_message`. KLIPY page urls
# 403 on scrape and have no oembed, so this local map is the only bridge. It is
# disk-backed (survives a relay restart, so old gifs still render on reload) and
# bounded so it can't grow without limit.
_KLIPY_CACHE_PATH = _os.path.join(_os.path.dirname(__file__), ".klipy_urls.json")
_KLIPY_CACHE_MAX = 8000
_KLIPY_API_TIMEOUT_S = 2.0     # klipy answers in ~250ms; generous but bounded
_KLIPY_PREWARM_CAP = 4        # at most N network resolves per page load
_KLIPY_PREWARM_BUDGET_S = 0.8  # hard first-paint budget; stragglers warm the cache
_klipy_item2gif: "OrderedDict[str, str]" = OrderedDict()  # page url -> direct gif
_klipy_gif2item: dict[str, str] = {}                      # direct gif -> page url
_klipy_loaded = False
_klipy_lock = threading.Lock()   # guards the maps under the prewarm thread pool


def _klipy_load() -> None:
    global _klipy_loaded
    if _klipy_loaded:
        return
    _klipy_loaded = True
    try:
        with open(_KLIPY_CACHE_PATH, encoding="utf-8") as fh:
            data = _json.load(fh)
        for item, gif in (data.get("item2gif") or {}).items():
            if item and gif:
                _klipy_item2gif[item] = gif
                _klipy_gif2item[gif] = item
    except (OSError, ValueError):
        pass


def _klipy_save() -> None:
    try:
        tmp = _KLIPY_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            _json.dump({"item2gif": dict(_klipy_item2gif)}, fh)
        _os.replace(tmp, _KLIPY_CACHE_PATH)
    except OSError:
        pass


def _klipy_remember(itemurl: str, gifurl: str) -> None:
    """Record a page-url <-> direct-gif pair from a served /gif-provider item.
    Caller flushes to disk once per batch via `_klipy_save`. Locked because the
    prewarm pool can call this from several threads at once."""
    if not itemurl or not gifurl:
        return
    _klipy_load()
    with _klipy_lock:
        if _klipy_item2gif.get(itemurl) == gifurl:
            _klipy_item2gif.move_to_end(itemurl)
            return
        _klipy_item2gif[itemurl] = gifurl
        _klipy_item2gif.move_to_end(itemurl)
        _klipy_gif2item[gifurl] = itemurl
        while len(_klipy_item2gif) > _KLIPY_CACHE_MAX:
            old_item, old_gif = _klipy_item2gif.popitem(last=False)
            _klipy_gif2item.pop(old_gif, None)


def _klipy_render_url(token: str) -> str | None:
    """The direct renderable gif url for one KLIPY reference, or None.

    A `static.klipy.com/....gif` token is already renderable; a
    `klipy.com/gifs/<slug>` page url resolves through the cache (None on a
    miss, so the caller leaves it as a plain text link)."""
    if not token:
        return None
    if "static.klipy.com" in token:
        return token
    if "klipy.com/gifs/" in token:
        _klipy_load()
        return _klipy_item2gif.get(token)
    return None


def _klipy_wire_url(gid: str) -> str:
    """What to POST as message content for a picked gif. Prefer the KLIPY page
    url (matches Fansly's own client on the wire); fall back to the direct gif
    (a valid klipy url) when the reverse map is cold, or build the page url from
    a bare legacy slug."""
    if not gid:
        return gid
    if "klipy.com/gifs/" in gid:
        return gid
    if "static.klipy.com" in gid:
        _klipy_load()
        return _klipy_gif2item.get(gid, gid)
    if "/" not in gid and " " not in gid:
        return f"https://klipy.com/gifs/{gid}"
    return gid


_klipy_unresolvable: set[str] = set()   # page urls KLIPY's API says don't exist


def _klipy_pick_gif(file_obj: Any) -> str | None:
    """Best direct media url from KLIPY's `file` tree — prefer a mid-size gif,
    then webp, then mp4. Falls back to a recursive scan so an unexpected shape
    still resolves."""
    if isinstance(file_obj, dict):
        for size in ("md", "hd", "sm", "xs", "original"):
            node = file_obj.get(size)
            if isinstance(node, dict):
                for kind in ("gif", "webp"):
                    variant = node.get(kind)
                    url = variant.get("url") if isinstance(variant, dict) else None
                    if url:
                        return url
    found = {"gif": None, "webp": None, "mp4": None}

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            u = o.get("url")
            if isinstance(u, str):
                low = u.lower()
                for ext in found:
                    if low.endswith("." + ext) and not found[ext]:
                        found[ext] = u
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(file_obj)
    return found["gif"] or found["webp"] or found["mp4"]


def _klipy_resolve_via_api(page_url: str) -> str | None:
    """Resolve a KLIPY page url to a direct gif via KLIPY's PUBLIC api
    (api.klipy.com/api/v1/gifs/<slug>). This is the only bridge for a page url
    we never served through our picker — a gif a FAN sent, one predating the
    cache, or an evicted one — since the page itself 403s on scrape. Called only
    on a local-cache miss; the result (or a permanent 'unresolvable' for a 404)
    is cached so a thread reload never re-hits the network for the same gif."""
    if page_url in _klipy_item2gif:
        return _klipy_item2gif[page_url]
    if page_url in _klipy_unresolvable:
        return None
    slug = page_url.rstrip("/").rsplit("/", 1)[-1]
    if not slug:
        return None
    try:
        resp = requests.get(f"https://api.klipy.com/api/v1/gifs/{slug}",
                            timeout=_KLIPY_API_TIMEOUT_S)
        if resp.status_code == 404:
            _klipy_unresolvable.add(page_url)   # genuinely gone — stop asking
            return None
        data = (resp.json() or {}).get("data") or {}
    except (requests.RequestException, ValueError):
        return None   # transient (timeout / bad body) — retry on the next render
    gif = _klipy_pick_gif(data.get("file") or {})
    if not gif:
        _klipy_unresolvable.add(page_url)
        return None
    _klipy_remember(page_url, gif)
    _klipy_save()
    return gif


def _klipy_prewarm(messages: list[dict]) -> None:
    """Resolve the page's uncached KLIPY page-urls into the cache BEFORE the pure
    reshapers run — bounded and parallel, so `of_message` never touches the
    network on the render path (this repo's architecture is text-first,
    media-late; a serial per-message resolve would invert that).

    Collect distinct unresolved klipy tokens, resolve up to `_KLIPY_PREWARM_CAP`
    of them in parallel, and wait only `_KLIPY_PREWARM_BUDGET_S` for first paint.
    Whatever finishes lands in the cache and renders now; stragglers keep going
    in the background and warm the cache for the next inbox poll (which is
    exactly how media already arrives late here). All-cached pages do no work."""
    pending: list[str] = []
    seen: set[str] = set()
    for m in messages:
        content = m.get("content") or ""
        if "klipy.com/gifs/" not in content:
            continue
        for tok in content.split():
            if ("klipy.com/gifs/" in tok and tok not in seen
                    and tok not in _klipy_unresolvable
                    and _klipy_render_url(tok) is None):
                seen.add(tok)
                pending.append(tok)
    if not pending:
        return
    pending = pending[:_KLIPY_PREWARM_CAP]
    ex = ThreadPoolExecutor(max_workers=len(pending))
    try:
        futures = [ex.submit(_klipy_resolve_via_api, u) for u in pending]
        wait(futures, timeout=_KLIPY_PREWARM_BUDGET_S)
    finally:
        # Don't join stragglers — each has its own bounded requests timeout and
        # warms the cache when it lands; the next poll will show those gifs.
        ex.shutdown(wait=False)


def _extract_klipy_gif(content: str) -> tuple[str | None, str]:
    """Split message content into (renderable gif url, remaining text).

    A gif send is content-only (no attachment); Fansly stores the KLIPY url as
    the message body. Pull the first cache-resolvable klipy token out as the gif
    and return whatever text remains (usually empty). This is PURE — cache-only,
    no network: `of_messages_page` pre-warms the cache via `_klipy_prewarm`
    first, and anything still unresolved stays a plain text link (a straggler
    resolve or the next poll fills it in)."""
    if not content or "klipy.com" not in content:
        return None, content or ""
    render: str | None = None
    kept: list[str] = []
    for tok in content.split():
        url = _klipy_render_url(tok)
        if url is not None:
            if render is None:
                render = url
            continue  # a resolved klipy token is the gif, not text
        kept.append(tok)
    return render, " ".join(kept)


# ---------------------------------------------------------------------------
# Pure reshapers  (Fansly object -> OnlyFans object)
# ---------------------------------------------------------------------------

def _iso(epoch_seconds: Any) -> str | None:
    """Fansly sends epoch **seconds**; OF sends ISO-8601 with tz. Convert."""
    if epoch_seconds in (None, 0, "0"):
        return None
    try:
        return datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _avatar_url(account: dict) -> str | None:
    """Dig a usable avatar URL out of a Fansly account object, or None.

    Fansly nests the avatar as a media object with `locations` / `variants`;
    the exact nesting varies, so probe the known shapes and give up cleanly
    rather than guess a URL that 404s."""
    avatar = account.get("avatar")
    if isinstance(avatar, str):
        return avatar
    if isinstance(avatar, dict):
        locs = avatar.get("locations")
        if isinstance(locs, list) and locs and isinstance(locs[0], dict):
            return locs[0].get("location")
        for variant in avatar.get("variants") or []:
            vlocs = variant.get("locations") if isinstance(variant, dict) else None
            if isinstance(vlocs, list) and vlocs and isinstance(vlocs[0], dict):
                return vlocs[0].get("location")
    return None


def _media_full_url(media: dict) -> str | None:
    """Full-res URL from a Fansly media object (locations[] > location > variant)."""
    locs = media.get("locations")
    if isinstance(locs, list) and locs and isinstance(locs[0], dict):
        if locs[0].get("location"):
            return locs[0]["location"]
    # NB: media["location"] is a bare STORAGE PATH (no host/signature) — never a
    # fetchable URL. Only signed locations[]/variant URLs count. (1a)
    for v in media.get("variants") or []:
        vl = v.get("locations") if isinstance(v, dict) else None
        if isinstance(vl, list) and vl and isinstance(vl[0], dict) and vl[0].get("location"):
            return vl[0]["location"]
    return None


def _media_variant_url(media: dict, target_w: int) -> str | None:
    """The variant with the largest width <= target_w — a lighter preview/thumb."""
    best: tuple[int, str] | None = None
    for v in media.get("variants") or []:
        if not isinstance(v, dict):
            continue
        w = v.get("width") or 0
        vl = v.get("locations")
        url = vl[0].get("location") if isinstance(vl, list) and vl and isinstance(vl[0], dict) else None
        if url and w <= target_w and (best is None or w > best[0]):
            best = (w, url)
    return best[1] if best else None


def of_media(account_media: dict) -> dict:
    """A Fansly `accountMedia` object -> OF message-media (OFMedia shape).

    OF's chat renderer resolves a still by walking DOWN files
    (source/full -> preview -> squarePreview -> thumb), so populate the sizes
    from Fansly's full `locations[]` + smaller `variants[]`. URLs are on
    cdn3.fansly.com and are Policy-signed (time-limited, NOT ip-bound)."""
    media = account_media.get("media") or {}
    full = _media_full_url(media)
    preview = _media_variant_url(media, 640) or full
    thumb = _media_variant_url(media, 320) or preview
    of_type = "photo" if media.get("type") == 1 else "video"

    def _var(u: str | None) -> dict | None:
        return {"url": u} if u else None

    # Id stays a STRING, like every other id this shim emits. Fansly ids are
    # snowflakes (~9.5e17) far above JS's 2^53 safe integer, and a bundle's
    # accountMedia ids differ only in their LAST DIGITS — 950839700124344320..324
    # all collapse to ...320 once the browser JSON.parses them as float64. That
    # collision is not theoretical: it silently keys five distinct tiles to one
    # entry in MediaTile's mediaIdToHash map (so tiles 2-5 would paint tile 1's
    # image) and hands React five identical `key`s for sibling tiles. Emitting
    # the id as a string keeps every media distinct end-to-end.
    mid = str(account_media.get("id") or "")
    return {
        "id": mid,
        "type": of_type,
        "url": full,
        "files": {
            "full": _var(full),
            "source": _var(full),
            "preview": _var(preview),
            "squarePreview": _var(preview),
            "thumb": _var(thumb),
        },
        "width": media.get("width"),
        "height": media.get("height"),
        # access=False is locked/unpurchased PPV — legitimately no signed URL.
        # The frontend reads canView===false to show a locked badge instead of a
        # broken image. (1b)
        "canView": bool(account_media.get("access")),
    }


def _note_overlays(account: dict) -> dict:
    """OF's per-fan overlays carried in Fansly's account `notes[]`:
    `displayName` = the creator's custom nickname (the singleton 12002 note),
    `notice` = the private fan note (the most-recent 12000 note). FanDrawer reads
    both off of-user as the source of truth, so without this a name/note changed
    on fansly.com or by an automation never surfaces.

    Emitted only when the account carries a `notes[]` key. The /account read path
    (get_user/list_users) always does. The lighter chat-list aggregation
    (/messaging/groups) carries it PER-FAN — present for fans who have notes
    (so the nickname shows on the chat list too), absent for fans who don't (so
    the keys are omitted and the drawer keeps its local-mirror fallback rather
    than an authoritative blank). A fan with a `notes` key but no 12002/12000
    yields "" for that field — an authoritative empty, which the sole consumer
    (FanDrawer, `?? "" || local`) treats identically to absent."""
    notes = account.get("notes")
    if not isinstance(notes, list):
        return {}
    nickname = ""
    note = ""
    for n in notes:
        if not isinstance(n, dict):
            continue
        if n.get("contentType") == 12002:
            nickname = n.get("note") or ""
        elif n.get("contentType") == 12000:
            note = n.get("note") or ""  # keep going -> ends on the newest note
    return {"displayName": nickname, "notice": note}


def of_user(account: dict) -> dict:
    """A Fansly account -> OF `withUser` / `me` user object.

    OF's fan object the inbox reads: id, name, username, avatar. `id` stays a
    string (both platforms use snowflakes; the relay normalizes to str). When the
    account carries notes[], also surface the custom nickname / fan note the way
    OF does (displayName / notice) — see `_note_overlays`."""
    return {
        "id": str(account.get("id") or ""),
        "name": account.get("displayName") or account.get("username") or "",
        "username": account.get("username") or "",
        "avatar": _avatar_url(account),
        # OF fields the inbox may glance at; safe defaults, not invented data.
        "isVerified": bool(account.get("flags", 0) & 16) or None,
        "isOnline": account.get("statusId") == 1 or None,
        **_note_overlays(account),
    }


def of_message(msg: dict, *, account_id: str,
               media: list | None = None, price: float = 0.0,
               previews: list | None = None,
               purchased_ids: AbstractSet[str] | None = None) -> dict:
    """A Fansly message -> OF message object.

    `account_id` (us) sets OF's outbound flag semantics: OF's UI reads
    `fromUser.id == me` to render a bubble as sent-by-us. Fansly's `senderId`
    carries the same fact, so the mapping is direct.

    Tips: Fansly rides the tip value in `totalTipAmount` (per-message), OF in
    `tipAmount` with `isTip`. Media: Fansly `attachments[]` -> OF `mediaCount`
    (the actual media objects are a separate resolve, out of inbox scope)."""
    sender = str(msg.get("senderId") or "")
    tip = msg.get("totalTipAmount") or 0
    attachments = msg.get("attachments") or []
    # A GIF rides as a KLIPY url in `content` (no attachment). Lift it into OF's
    # `giphyId` so the bubble renders the gif instead of printing the url. On a
    # cache miss the url stays in `text` as a plain link (graceful).
    giphy_render, text = _extract_klipy_gif(msg.get("content") or "")
    out = {
        "id": str(msg.get("id") or ""),
        "text": text,
        "fromUser": {"id": sender},
        "createdAt": _iso(msg.get("createdAt")),
        "isTip": bool(tip),
        "tipAmount": tip,
        # Recovered from the attachments' grants, NOT from the message (Fansly
        # always reports the message at 0) — see _attachment_price_usd.
        "price": price,
        "isFree": not price,
        # A PPV is LOCKED until the fan actually buys it. This was hardcoded
        # True, which made MessageList's `unlocked = isPPV && (isOpened ||
        # isPaid)` true for every priced message — so a $5 PPV we had just
        # sent rendered a green "✓ $5.00 unlocked" chip while the fan had paid
        # nothing. Telling a chatter a fan has paid when they have not is the
        # worst kind of wrong here, so a priced message reports NOT opened
        # until we have positive evidence of a purchase. Free messages keep
        # True: isPPV is false for them, so the chip never renders anyway.
        #
        # 🚨 DO NOT wire the grant's `purchased` field to this. It looks exactly
        # like the signal you want and it is NOT: it is VIEWER-relative, and the
        # viewer is us, the owner. Measured live across this account's sends —
        # `purchased: true` on every grant, INCLUDING the free ones, right
        # beside `access: true` / `whitelisted: true`. It answers "can the
        # caller see this", not "did the fan pay", so trusting it would restore
        # the false "✓ unlocked" chip in one line.
        #
        # The REAL signal is the notifications feed, not this object: a fan
        # paying emits a `2007`/`2008` ("Media Purchases") row whose
        # `correlationId` is the **accountMedia grant id** they bought and
        # whose `correlationGroupId` is the **buyer**. `purchased_ids` is that
        # set, scoped to this thread's fan by the caller — so a priced message
        # opens only once a purchase by THIS fan names one of its own grants.
        # Confirmed live 2026-09-02: a real $3 unlock on fan
        # 951404711209099264 produced type 2007, correlationId
        # <grant id> (a grant on the message), metadata
        # {"accountMediaPrice":3000} — 1/1000-dollar units, like every other
        # Fansly price (FANSLY_PRICE_UNIT). Absent that evidence a priced
        # message stays locked: telling a chatter a fan paid when they have
        # not is the worst kind of wrong here.
        "isOpened": (not price) or _is_purchased(msg, media, purchased_ids),
        "isNew": False,
        "media": media or [],
        # Which tiles ride FREE on a paid message — see _preview_grant_ids.
        "previews": previews or [],
        # A bundle is ONE attachment that expands to N media, so count the
        # resolved media when we have them (else fall back to attachment count
        # for previews that don't resolve media). (peer #5)
        "mediaCount": len(media) if media else len(attachments),
        # Reply linkage. The field is `replyToMessageId` — the name OFMessage
        # declares (app/lib/relay.ts) and the ONLY one the chat reads
        # (MessageList resolves `m.replyToMessageId` through its id map). This
        # emitted `replyOnMessageId`, which nothing in the app reads, so a
        # Fansly quote-reply silently rendered as a plain message with no
        # quoted bubble above it. STRING, like every other id here: a Fansly
        # snowflake exceeds JS's 2^53.
        "replyToMessageId": str(msg["inReplyTo"]) if msg.get("inReplyTo") else None,
    }
    if giphy_render:
        # `giphyId` carries the full renderable url; the frontend's giphyUrl()
        # passes an http(s) value straight through (only bare ids get the
        # media.giphy.com wrapper).
        out["giphyId"] = giphy_render
    return out


def of_chat_row(membership: dict, fan: dict | None, last_message: dict | None,
                *, account_id: str) -> dict:
    """One Fansly group (+ its fan account + last message) -> an OF `/chats` row.

    Identity mapping — the one real subtlety (see PARITY_SPEC §2): OF keys a
    chat by the fan's id; Fansly keys it by `groupId` (its own snowflake). The
    row's `id` is the **groupId** (the opaque thread key `get_messages` pages
    on), while `withUser.id` is the **fan's accountId** (what profile / notes /
    avatar key on). They are deliberately different here, unlike OF."""
    unread = int(membership.get("unreadCount") or 0)
    # OF zeroes a chat's unread the moment WE reply; Fansly does NOT — its
    # `unreadCount` keeps counting fan messages we never explicitly marked read,
    # even when our own message is the newest in the thread. Emitted verbatim it
    # pinned the roster badge BLUE forever: `_roster_counts_from_rows` tests
    # unread FIRST and `continue`s, so a replied-to chat could never reach the
    # orange "owe reply" state, and opening one looked like a no-op because the
    # next poll restored the blue.
    #
    # Fansly's own two pointers settle who spoke last, with no extra call:
    #   lastMessageId == lastUnreadMessageId -> the newest message IS an unread
    #       fan message: genuinely blue, leave it.
    #   they DIFFER                          -> something newer than the newest
    #       unread exists (our reply): OF would have cleared it, so we do.
    # Verified live: lexi 951212311962460160 vs ...5751051792384 (our reply ->
    # cleared); bonnie 951007747011272705 == itself (fan last -> stays 2).
    # Both ids must be present — absent pointers mean "can't tell", and the safe
    # answer is to keep the unread rather than hide a waiting fan.
    _last_id = str(membership.get("lastMessageId") or "")
    _last_unread_id = str(membership.get("lastUnreadMessageId") or "")
    if unread and _last_id and _last_unread_id and _last_id != _last_unread_id:
        unread = 0
    fan_obj = of_user(fan) if fan else {
        "id": str(membership.get("partnerAccountId") or ""),
        "name": membership.get("partnerUsername") or "",
        "username": membership.get("partnerUsername") or "",
        "avatar": None,
    }
    return {
        "id": str(membership.get("groupId") or ""),
        "withUser": fan_obj,
        "lastMessage": of_message(last_message, account_id=account_id) if last_message else None,
        "unreadMessagesCount": unread,
        "hasUnreadMessages": unread > 0,
        "canSendMessage": True,
        "canSendMedia": True,
        "isMutedNotifications": False,
        "isPinned": bool(membership.get("flags", 0) & 4),
    }


def of_chats_page(groups_response: dict, *, account_id: str, limit: int) -> dict:
    """Fansly `/messaging/groups` `{data, aggregationData}` -> OF `/chats` page.

    `/messaging/groups` is a join: `data[]` are the memberships, the full fan
    objects live in `aggregationData.accounts[]`. Stitch them by id. Fansly has
    no `hasMore` flag on this endpoint, so infer it from a full page."""
    data = groups_response.get("data") or []
    agg = groups_response.get("aggregationData") or {}
    accounts_by_id = {str(a.get("id")): a for a in (agg.get("accounts") or [])}
    # Last messages sometimes ride along in aggregationData; index if present.
    messages_by_id = {str(m.get("id")): m for m in (agg.get("messages") or [])}

    rows = []
    for membership in data:
        fan = accounts_by_id.get(str(membership.get("partnerAccountId")))
        last = messages_by_id.get(str(membership.get("lastMessageId")))
        rows.append(of_chat_row(membership, fan, last, account_id=account_id))

    return {"list": rows, "hasMore": len(data) >= limit}


def _media_indexes(payload: dict) -> tuple[dict, dict]:
    """Index a Fansly payload's accountMedia[] + accountMediaBundles[] by id.

    /message and /timelinenew both ride media the same way, so both the inbox
    and the posts reshaper share this."""
    return (
        {str(m.get("id")): m for m in (payload.get("accountMedia") or [])},
        {str(b.get("id")): b for b in (payload.get("accountMediaBundles") or [])},
    )


def _bundle_media_ids(bundle: dict) -> list[str]:
    """A bundle's accountMedia ids in display order."""
    content = bundle.get("bundleContent")
    if content:
        return [str(x.get("accountMediaId"))
                for x in sorted(content, key=lambda x: x.get("pos") or 0)]
    return [str(x) for x in (bundle.get("accountMediaIds") or [])]


def _price_metadata(row: dict) -> dict:
    """A notification's `metadata` as a dict — it arrives as a JSON *string* —
    or {} when it is absent or unparseable. Shared so the price reader and the
    "did the shape drift" check can never disagree about what the row said."""
    meta = row.get("metadata")
    if isinstance(meta, str):
        try:
            meta = _json.loads(meta)
        except (ValueError, TypeError):
            return {}
    return meta if isinstance(meta, dict) else {}


def _notif_price_usd(row: dict) -> float:
    """The dollar amount a purchase notification row reports, or 0.0.

    OBSERVED live 2026-09-02 on a real $3 sale: a `2007` row carries
    `metadata` as a JSON *string* holding `{"accountMediaPrice": 3000}` —
    1/1000-dollar units, like every other Fansly price (FANSLY_PRICE_UNIT).
    This supersedes the older "metadata was null on every row ever captured"
    note: it is null on the social codes, and priced on the money ones.

    Returns 0.0 — never a guess — when the field is absent or unparseable, so
    a shape change costs the amount on the row rather than inventing revenue.
    """
    meta = _price_metadata(row)
    # PRESENCE, not truthiness: a row that explicitly reports price 0 is a free
    # unlock, and must read as $0.00 rather than falling through to a later key.
    for key in _PRICE_METADATA_KEYS:
        if key not in meta:
            continue
        try:
            return int(meta[key]) / FANSLY_PRICE_UNIT
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _grant_price_usd(account_media: dict) -> float:
    """The PPV price carried by ONE accountMedia grant, in dollars.

    Fansly prices the MEDIA, never the message: `/message` always reports the
    message itself at price 0, and the real figure sits in the grant's
    `permissions.permissionFlags[].price` in 1/1000-dollar units (see
    FANSLY_PRICE_UNIT). Reading only the message — which is what we did until
    a live check found two real $101 sends reading back as free — makes OF's
    `isPPV` (msg.price > 0) false, so the composer showed no lock chip, no
    PAID badge and no price on a message the fan really was charged for.

    Free grants carry an empty permissionFlags list, so this returns 0.0 and
    the message stays honestly free."""
    flags = ((account_media or {}).get("permissions") or {}).get("permissionFlags") or []
    amounts = [p.get("price") or 0 for p in flags if isinstance(p, dict)]
    top = max(amounts) if amounts else 0
    return (top / FANSLY_PRICE_UNIT) if top else 0.0


def _purchasable_ids(item: dict, media: list | None) -> set:
    """Every id a purchase of THIS message could be recorded against.

    Two shapes, because a Fansly message can carry either:
      • the resolved media's own accountMedia GRANT ids (`of_media` puts the
        grant id in each tile's `id`), and
      • the raw attachment `contentId`s — which for a contentType:2 attachment
        is the BUNDLE id, an id that never appears in `media[]` because
        `_resolve_attachments` expands a bundle into its members and drops it.

    Both are included deliberately. A single-media buy is CONFIRMED live to
    report the grant id (2026-09-02: grant <grant id>, $3). A BUNDLE
    buy has never happened on this account, so whether Fansly's 2007 row names
    the bundle or one member grant is UNVERIFIED — and if it names the bundle,
    matching only on `media[]` would leave every bundled PPV permanently
    locked, which is the exact bug this feature exists to remove. Indexing
    both costs one set union and is correct under either answer.

    This cannot create a false unlock: every id here belongs to this message,
    so a match still means this fan paid for something on it."""
    ids = {str(m.get("id")) for m in (media or []) if isinstance(m, dict) and m.get("id")}
    for a in item.get("attachments") or []:
        cid = str(a.get("contentId") or "")
        if cid:
            ids.add(cid)
    return ids


def _is_purchased(item: dict, media: list | None,
                  purchased_ids: AbstractSet[str] | None) -> bool:
    """Did this fan buy something riding on this message?

    `purchased_ids` is the set of ids this thread's fan has a `2007`/`2008`
    purchase notification for. One match unlocks the whole message, which is
    what OF's `isOpened` means: OF locks per MESSAGE, Fansly charges per
    GRANT, and a fan cannot buy half a message — paying for any locked part of
    it opens the bubble.

    Conservative by construction: no purchase set, or no overlap, leaves the
    message honestly locked."""
    if not purchased_ids:
        return False
    return bool(_purchasable_ids(item, media) & set(purchased_ids))


def _attachment_price_usd(item: dict, am_by_id: dict, bundle_by_id: dict) -> float:
    """The price OF would call the MESSAGE's price: the dearest grant among
    its attachments.

    `max` rather than a sum: OF's `price` is what the fan pays to unlock the
    message, and Fansly charges per grant with the teaser grants free — so a
    $20 set with one free preview is a $20 message, not $40. A message whose
    attachments are all free is free."""
    prices = []
    for a in item.get("attachments") or []:
        cid = str(a.get("contentId") or "")
        if a.get("contentType") == 2 and cid in bundle_by_id:
            prices += [_grant_price_usd(am_by_id[m])
                       for m in _bundle_media_ids(bundle_by_id[cid]) if m in am_by_id]
        elif cid in am_by_id:
            prices.append(_grant_price_usd(am_by_id[cid]))
    return max(prices) if prices else 0.0


def _preview_grant_ids(item: dict, am_by_id: dict, bundle_by_id: dict) -> list:
    """OF's `previews` — the attachments shown FREE as the teaser on a paid
    message.

    Derived, because Fansly has no such field: price lives per-grant, so on a
    priced message a grant carrying NO price IS the teaser. Without this the
    chat labelled every tile of a teaser send red "PPV-locked — fan hasn't
    unlocked this tile", including the one the fan can actually see for free
    (MessageList reads `msg.previews` to pick grey-vs-red, :1001/:1095).

    Returns GRANT ids, matching what `of_media` puts in each tile's `id` — the
    accountMedia id, not the underlying vault mediaId. Empty for a free
    message: with nothing locked there is no teaser to distinguish."""
    ids, any_priced = [], False
    for a in item.get("attachments") or []:
        cid = str(a.get("contentId") or "")
        grants = []
        if a.get("contentType") == 2 and cid in bundle_by_id:
            grants = [am_by_id[m] for m in _bundle_media_ids(bundle_by_id[cid])
                      if m in am_by_id]
        elif cid in am_by_id:
            grants = [am_by_id[cid]]
        for g in grants:
            if _grant_price_usd(g) > 0:
                any_priced = True
            elif g.get("id"):
                ids.append(str(g["id"]))
    return ids if any_priced else []


def _resolve_attachments(item: dict, am_by_id: dict, bundle_by_id: dict) -> list:
    """attachments[] -> OF media[]. contentType 1 = one accountMedia, 2 = a
    bundle that expands to several (in `pos` order)."""
    out = []
    for a in item.get("attachments") or []:
        cid = str(a.get("contentId") or "")
        if a.get("contentType") == 2 and cid in bundle_by_id:
            for amid in _bundle_media_ids(bundle_by_id[cid]):
                if amid in am_by_id:
                    out.append(of_media(am_by_id[amid]))
        elif cid in am_by_id:
            out.append(of_media(am_by_id[cid]))
    return out


def of_post(post: dict, *, media: list | None = None) -> dict:
    """A Fansly `/timelinenew` post -> OF's post object.

    `id` is a STRING for the same reason every other id here is: Fansly
    snowflakes exceed JS's 2^53. `OFPost.id` in usePosts.ts is still typed
    `number` — widening it is a separate change with its own blast radius (see
    the VaultList.id note in FANSLY_PARITY_STATUS.md); the frontend keys and
    compares posts by value, so a string is safe at runtime today."""
    return {
        "id": str(post.get("id") or ""),
        "text": post.get("content") or "",
        "rawText": post.get("content") or "",
        # Fansly has no per-post price on the timeline object; PPV lives on the
        # attached bundle. Leave it null rather than implying "free".
        "price": None,
        "postedAt": _iso(post.get("createdAt")),
        "postedAtPrecise": _iso(post.get("createdAt")),
        "likesCount": post.get("likeCount") or 0,
        "commentsCount": post.get("replyCount") or 0,
        "mediaCount": len(media if media is not None else (post.get("attachments") or [])),
        "media": media or [],
        # OF marks free teasers on a paid post; Fansly has no equivalent on the
        # timeline object, so send an empty list rather than a guess.
        "previews": [],
    }


def of_posts_page(timeline: dict, *, limit: int) -> dict:
    """Fansly `/timelinenew/<accountId>` -> OF's posts page.

    Same media plumbing as the inbox: attachments index into accountMedia[] /
    accountMediaBundles[], resolved through the shared `_resolve_attachments`."""
    posts = timeline.get("posts") or []
    am_by_id, bundle_by_id = _media_indexes(timeline)
    rows = [of_post(p, media=_resolve_attachments(p, am_by_id, bundle_by_id))
            for p in posts]
    return {"list": rows, "hasMore": len(posts) >= limit}


def of_messages_page(message_response: dict, *, account_id: str, limit: int,
                     purchased_ids: AbstractSet[str] | None = None) -> dict:
    """Fansly `/message?groupId=` `{messages, ...}` -> OF messages page.

    Fansly returns newest-first, which matches OF's `order=desc` default."""
    msgs = message_response.get("messages") or []
    # Warm the KLIPY cache for this page's gifs off the reshape path (bounded +
    # parallel) so of_message stays network-free — see _klipy_prewarm.
    _klipy_prewarm(msgs)
    # /message rides media alongside text. A contentType:1 attachment's
    # contentId indexes accountMedia[]; a contentType:2 attachment's contentId
    # is a BUNDLE in accountMediaBundles[] that expands to several accountMedia.
    am_by_id, bundle_by_id = _media_indexes(message_response)

    def _resolve(msg: dict) -> list:
        return _resolve_attachments(msg, am_by_id, bundle_by_id)

    return {
        "list": [of_message(m, account_id=account_id, media=_resolve(m),
                            price=_attachment_price_usd(m, am_by_id, bundle_by_id),
                            previews=_preview_grant_ids(m, am_by_id, bundle_by_id),
                            purchased_ids=purchased_ids)
                 for m in msgs],
        "hasMore": len(msgs) >= limit,
    }


def _klipy_to_giphy(items: Any, *, offset: int = 0) -> dict:
    """Fansly `/gif-provider/*` list -> OF's GiphyResponse ({data, pagination}).

    Each Fansly item: {itemurl (=klipy.com/gifs/<slug>, what a send posts as
    content), gifUrl, tinyGifUrl, mp4Url, title, width, height, provider}. OF's
    picker reads GiphyItem.id (sent back as giphy_id) + images.*.url."""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        itemurl = it.get("itemurl") or ""
        slug = itemurl.rstrip("/").rsplit("/", 1)[-1] if itemurl else ""
        gif = it.get("gifUrl") or ""
        tiny = it.get("tinyGifUrl") or gif
        mp4 = it.get("mp4Url") or ""
        w = str(it.get("width") or "")
        h = str(it.get("height") or "")

        def _v(url: str, with_mp4: bool = False) -> dict:
            v = {"url": url or "", "width": w, "height": h}
            if with_mp4:
                v["mp4"] = mp4
            return v

        # Remember the page-url <-> direct-gif pair so `of_message` can turn the
        # klipy url Fansly echoes back into a renderable gif, and `send_message`
        # can post the native page url on the wire.
        _klipy_remember(itemurl, gif)
        out.append({
            # `id` is what the picker sends back as `giphy_id` AND what the
            # optimistic bubble renders via giphyUrl(). KLIPY's send-token (the
            # page url) is unrenderable, so carry the DIRECT gif here; the wire
            # url is recovered from it in send_message via the reverse map.
            "type": "gif",
            "id": gif or itemurl or slug,
            "url": itemurl,
            "slug": slug,
            "title": it.get("title") or "",
            "images": {
                "original": _v(gif, with_mp4=True),
                "fixed_height": _v(tiny),
                "fixed_height_small": _v(tiny),
                "fixed_width": _v(tiny),
                "downsized": _v(tiny),
                "preview_gif": _v(tiny),
            },
        })
    if out:
        _klipy_save()
    return {
        "data": out,
        "pagination": {"total_count": len(out), "count": len(out), "offset": offset},
    }


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

class FanslyShimClient(VaultMixin, FanslyClient):
    """A FanslyClient that also answers OF's inbox read methods in OF's shape.

    Drop-in for the relay: `_load_client` returns this for `platform=="fansly"`
    accounts, and `_assemble_chats_page(client, ...)` works unchanged."""

    # -- OF-client compatibility surface ------------------------------------
    # The relay reads these off a generic `client` in shared paths it never
    # branched by platform: roster counts (`str(client.user_id)`), session
    # status / proxy display (`client.proxy_label` / `.proxy_url` /
    # `.egress_ip()` / `.account_id`). OFClient exposes them as instance attrs.
    # Fansly keys identity on the account id and has no proxy wiring, so we
    # answer with the Fansly-correct equivalents rather than let the shared
    # code AttributeError.

    @property
    def user_id(self) -> str:
        """Our own creator id. OF names it `user_id`; on Fansly it's the account
        id — what `_roster_counts_from_rows` compares `fromUser.id` against to
        tell our sent messages from the fan's."""
        return str(self.session.account_id)

    @property
    def account_id(self) -> str:
        return str(self.session.account_id)

    proxy_label = None    # Fansly path attaches no proxy
    proxy_url = None
    x_of_rev = None       # OF revision concept; not applicable to Fansly

    @property
    def timeout_s(self):
        """OF-client alias the /img proxy reads; Fansly stores it as `timeout`."""
        return self.timeout

    def egress_ip(self) -> None:
        """No proxy attached, so no distinct egress IP to report."""
        return None

    # -- OF-shaped reads ----------------------------------------------------

    def me(self) -> dict:  # type: ignore[override]
        """OF `/users/me` shape. (FanslyClient.me returns the raw Fansly obj;
        the relay wants OF's `{id, name, username, avatar}`.)"""
        account = self._request("GET", "/account/me")["account"]
        return of_user(account)

    def list_chats(self, *, limit: int = 10, offset: int = 0,
                   order: str = "recent", filter: str | None = None,
                   list_id: int | str | None = None,
                   query: str | None = None,
                   skip_users: str | None = "all") -> dict:
        """OF `/chats` shape. Accepts the full OF kwarg set so it is a literal
        drop-in for `_assemble_chats_page`; Fansly-unsupported filters
        (pinned/priority, folders) are honored where cheap, ignored otherwise.

        `filter="unread"` maps to Fansly's `flags` unread selector; `query` maps
        to Fansly's `search`. `list_id` (OF folders) has no Fansly equivalent in
        the inbox scope and is ignored (returns the unfiltered page)."""
        params = {
            "sortOrder": "1",
            "flags": "2" if filter == "unread" else "0",
            "subscriptionTierId": "",
            "listIds": "",
            "search": query or "",
            "limit": str(limit),
            "offset": str(offset),
        }
        groups = self._request("GET", "/messaging/groups", params=params)
        page = of_chats_page(groups, account_id=self.session.account_id, limit=limit)
        # Remember the groupIds we just emitted so a later get_messages on one of
        # them skips the find_group_with scan (see _as_group_id).
        self._known_group_ids.update(
            r["id"] for r in page["list"] if r.get("id")
        )
        # The frontend addresses a thread by the fan's id (OF convention), not
        # our groupId, so cache fan -> group from this same response. That makes
        # get_messages / send resolution O(1) and correct even for threads older
        # than find_group_with's 100-row scan window.
        for m in (groups.get("data") or []):
            fan, grp = m.get("partnerAccountId"), m.get("groupId")
            if fan and grp:
                self._fan_to_group[str(fan)] = str(grp)
                # The reverse direction too: a media send has to whitelist the
                # recipient by accountId, and all it is handed is the groupId.
                self._group_to_fan[str(grp)] = str(fan)
        self._backfill_last_messages(page["list"])
        return page

    def get_messages(self, chat_id: str | int, *, limit: int = 25,
                     order: str = "desc", before_id: str | int | None = None,
                     offset: int | None = None) -> dict:
        """OF `/chats/{chat_id}/messages` shape.

        `chat_id` is the Fansly groupId (what `of_chat_row` put in row `id`). We
        also tolerate being handed a fan accountId — resolve it to a group — so
        callers that assume OF's `chat_id == fan_id` still work."""
        group_id = self._as_group_id(chat_id)
        params: dict[str, Any] = {"groupId": str(group_id), "limit": str(limit)}
        if before_id is not None:
            params["before"] = str(before_id)
        resp = self._request("GET", "/message", params=params)
        # Which grants has the fan on the OTHER side of this thread paid for?
        # Resolved from the notifications feed (the only creator-visible
        # purchase signal — see of_message's isOpened note) and scoped to that
        # fan, so one fan's purchase never unlocks another's bubble.
        fan_id = self._fan_id_for_group(group_id)
        purchased = self._purchases_by_fan().get(str(fan_id or ""), frozenset())
        return of_messages_page(resp, account_id=self.session.account_id, limit=limit,
                                purchased_ids=purchased)

    def chat_folders(self, *, limit: int = 10, offset: int = 0,
                     filter: str = "can_pin_chat") -> dict:
        """OF chat sidebar folders. Fansly has lists, but folder-pinning is out
        of inbox scope — return OF's empty shape so the sidebar renders with no
        folders rather than erroring."""
        return {"list": [], "hasMore": False}

    def get_user(self, who: str | int) -> dict:
        """OF `/users/{id}` — one fan profile. Fansly: GET /account?ids=. Returns
        {} when the id doesn't resolve (OF returns an empty-ish object too)."""
        accounts = self._request("GET", "/account", params={"ids": str(who)})
        return of_user(accounts[0]) if accounts else {}

    def list_users(self, ids: Any, *, view: str = "m") -> dict:
        """OF `/users/list` — batch fan profiles keyed by id string
        (`{"<id>": {...}}`). Fansly: GET /account?ids=a,b,c."""
        id_list = [str(i) for i in (ids or [])]
        if not id_list:
            return {}
        accounts = self._request("GET", "/account",
                                 params={"ids": ",".join(id_list)}) or []
        out: dict[str, dict] = {}
        for a in accounts:
            u = of_user(a)
            if u.get("id"):
                out[str(u["id"])] = u
        return out

    # ── Fan note + custom name (Fansly "notes") ────────────────
    # Fansly models both as `notes` on the account object
    # (GET /account?ids= -> notes[]): contentType 12000 is a freeform note,
    # 12002 is the singleton "Custom Username". Create via POST /notes; edit an
    # existing one via POST /notes/edit {id,...}. (captured: notesandnicknames.har)
    _NOTE_TYPE_FREEFORM = 12000
    _NOTE_TYPE_CUSTOM_NAME = 12002

    def _fan_notes(self, user_id: str | int) -> list:
        """The `notes[]` Fansly hangs off a fan's account object."""
        accounts = self._request("GET", "/account", params={"ids": str(user_id)}) or []
        notes = accounts[0].get("notes") if accounts else None
        return notes if isinstance(notes, list) else []

    def _upsert_note(self, user_id: str | int, content_type: int,
                     title: str, note: str) -> dict:
        """Edit the fan's existing note of this type (the most recent, so a
        re-run overwrites rather than piling up duplicates), else create one."""
        existing = None
        for n in self._fan_notes(user_id):
            if isinstance(n, dict) and n.get("contentType") == content_type and n.get("id"):
                existing = n  # keep the LAST match — the most-recent note
        if existing:
            return self._request("POST", "/notes/edit", json_body={
                "id": str(existing["id"]),
                "contentType": content_type,
                "contentId": str(user_id),
                "title": title,
                "note": note,
            })
        return self._request("POST", "/notes", json_body={
            "contentType": content_type,
            "contentId": str(user_id),
            "title": title,
            "note": note,
        })

    def set_fan_custom_name(self, user_id: str | int, name: str) -> dict:
        """OF's per-fan custom nickname. Fansly: the singleton 12002 note
        ("Custom Username"). Edit-or-create; empty string clears it."""
        return self._upsert_note(user_id, self._NOTE_TYPE_CUSTOM_NAME,
                                 "Custom Username", name or "")

    def set_fan_note(self, user_id: str | int, note: str) -> dict:
        """OF's private per-fan note. Fansly keeps a LIST of freeform (12000)
        notes where OF has one, so we edit the fan's most-recent freeform note
        (else create) to hold single-note overwrite semantics. The first line
        doubles as the Fansly title (its notes UI is title-first)."""
        text = note or ""
        title = (text.splitlines()[0] if text else "")[:90]
        return self._upsert_note(user_id, self._NOTE_TYPE_FREEFORM, title, text)

    def get_chat(self, chat_id: str | int) -> dict:
        """OF `/chats/{id}` single-thread shape (one row)."""
        group_id = self._as_group_id(chat_id)
        detail = self._request("GET", f"/group/{group_id}/")
        fan_id = next(
            (str(u.get("userId")) for u in detail.get("users", [])
             if str(u.get("userId")) != str(self.session.account_id)),
            "",
        )
        fan = None
        if fan_id:
            accounts = self._request("GET", "/account", params={"ids": fan_id})
            fan = accounts[0] if accounts else None
        membership = {
            "groupId": str(group_id),
            "partnerAccountId": fan_id,
            "unreadCount": 0,
            "flags": detail.get("groupFlags", 0),
        }
        return of_chat_row(membership, fan, detail.get("lastMessage"),
                           account_id=self.session.account_id)

    # -- last-message backfill ---------------------------------------------

    def _last_message_for_group(self, group_id: str | int) -> dict | None:
        """The newest message in a group, OF-shaped, fetched by *known*
        groupId.

        NB the limit: ``limit=1`` is NOT usable — Fansly answers it with an
        EMPTY ``messages`` array (verified live on a 12-message thread:
        limit=1 -> 0 rows, limit=25 -> 12). That silently blanked the inbox
        preview for such threads, and it also disabled BOTH roster-badge rules,
        which key off ``lastMessage``: a chat with no resolved last message can
        be neither "we replied" (clear) nor "fan spoke last" (orange), so it
        stuck on blue. Ask for a small page and take the newest.

        Deliberately does NOT go through ``_as_group_id``: the caller already
        holds a real groupId, and the resolve path would waste a 100-group
        scan. Best-effort — any failure yields None so one dead thread never
        breaks the whole list page."""
        try:
            resp = self._request(
                "GET", "/message",
                # NOT limit=1 — see the note above.
                params={"groupId": str(group_id), "limit": "25"},
            )
        except Exception:
            return None
        msgs = (resp or {}).get("messages") or []
        if not msgs:
            return None
        return of_message(msgs[0], account_id=self.session.account_id)

    def _backfill_last_messages(self, rows: list[dict], *, cap: int = 30) -> None:
        """Fill each row's ``lastMessage`` preview.

        Fansly's ``/messaging/groups`` omits the last message inline — the
        membership carries ``lastMessageId`` (often null) and there is no
        ``aggregationData.messages`` — so the inbox list preview would be blank.
        We fill it per-thread with one cheap ``/message?limit=1`` call each.

        Cost: this is N+1 in the page size, but the page is already bounded by
        ``limit`` and we hard-cap at ``cap`` rows; calls are sequential and
        best-effort. If a large inbox ever makes this the latency bottleneck,
        parallelize here — not before (YAGNI)."""
        for row in rows[:cap]:
            if row.get("lastMessage") is not None:
                continue
            gid = row.get("id")
            if gid:
                row["lastMessage"] = self._last_message_for_group(gid)

    # -- OF-shaped write ----------------------------------------------------

    # -- media attachment (captured: "vault and send.har") ------------------
    # Attaching vault media is TWO calls, not one. A vault mediaId is not a
    # sendable thing — it is a file in your library. What a message carries is
    # an **accountMedia** envelope: a per-send grant naming who may view the
    # file and, for PPV, what it costs. So:
    #
    #   1. POST /account/media  [{mediaId, whitelist, permissions, ...}]
    #        -> response[0]["id"]   ... the accountMedia id
    #   2. POST /message  {attachments: [{contentId: <that id>, contentType: 1}]}
    #
    # Passing the raw vault mediaId straight through as contentId is ACCEPTED by
    # the API and delivers a dead attachment, so the two steps are mandatory,
    # not an optimization. A fresh grant is minted per send because the grant is
    # what carries the recipient whitelist and the price.
    #
    # Constant in all three captured sends: whitelist = [us, recipient] (Fansly
    # drops us from the echoed copy, ownership being implicit), envelope
    # permissionFlags 8, and top-level price 0 — a PPV's real price lives in
    # `permissions.permissionFlags`, NOT in that top-level field.

    _MEDIA_CONTENT_TYPE = 1        # 1 = a single accountMedia (2 = a bundle)
    _MEDIA_PERMISSION_FLAGS = 8    # constant across all 3 captured sends

    def _price_permissions(self, price_usd: Any) -> dict:
        """One attachment's `permissions`: empty when free, a priced grant when
        PPV.

        The captured paid send is `type 0, flags 3`, carrying the price BOTH as
        an int and, redundantly, inside a doubly-encoded metadata blob —
        `metadata` is a JSON string whose "1" key holds *another* JSON string.
        That double encoding is Fansly's own; the separators are pinned so our
        body matches the browser's bytes rather than merely parsing the same."""
        amount = _fansly_price(price_usd)
        if amount <= 0:
            return {"permissionFlags": []}
        inner = _json.dumps({"price": amount}, separators=(",", ":"))
        return {"permissionFlags": [{
            "type": 0,
            "flags": 3,
            "metadata": _json.dumps({"1": inner}, separators=(",", ":")),
            "price": amount,
        }]}

    def _attach_media(self, media_ids: list, *, fan_id: str,
                      price_usd: Any = 0, preview_ids: Any = None) -> list[str]:
        """Vault mediaId[] -> accountMedia id[], ready to hang off a message.

        One POST per media, matching the capture exactly rather than batching
        into the array the endpoint nominally accepts: only the single-entry
        form has ever been observed, and an entry the server silently dropped
        would surface as a partial send nobody noticed. Attachment counts are
        small, so the extra round-trips are cheap insurance.

        That per-media grant is also what makes OF's `previews` expressible.
        OF sends a subset of the attachments UNLOCKED as the teaser on a paid
        message; since price lives on the grant and every attachment gets its
        own, a media listed in `preview_ids` simply gets a FREE grant while its
        siblings get priced ones. Same message, mixed locks — which is exactly
        what OF renders. (Fansly's own `previewId` field is a different thing:
        a blurred stand-in image ON a locked grant, not a free sibling, and the
        browser left it null in every captured send.)"""
        us = str(self.session.account_id)
        priced = self._price_permissions(price_usd)
        free = {"permissionFlags": []}
        previews = {str(p) for p in (preview_ids or [])}
        content_ids: list[str] = []
        for mid in media_ids:
            body = [{
                "mediaId": str(mid),
                "previewId": None,
                "permissionFlags": self._MEDIA_PERMISSION_FLAGS,
                "price": 0,
                "whitelist": [{"accountId": us, "permissionFlags": 0},
                              {"accountId": str(fan_id), "permissionFlags": 0}],
                "permissions": free if str(mid) in previews else priced,
                "tags": [],
            }]
            resp = self._request("POST", "/account/media", json_body=body)
            row = (resp or [{}])[0] if isinstance(resp, list) and resp else {}
            cid = row.get("id")
            if not cid:
                raise FanslyAPIError(
                    f"/account/media accepted media {mid} but returned no "
                    f"accountMedia id, so there is nothing to attach."
                )
            content_ids.append(str(cid))
        return content_ids

    def send_message(self, chat_id: str | int, text: str | None = None, *,
                     locked_text: str | None = None, price: int = 0,
                     media_files: Any = None, previews: Any = None,
                     is_forward: bool = False,
                     reply_to_message_id: str | int | None = None,
                     tagged_users: Any = None, giphy_id: str | None = None,
                     auto_tag: bool = False, _broadcast: bool = False,
                     **_ignored: Any) -> dict:
        """OF-signature send. The relay's send handler calls this with OFClient's
        full kwarg set (`text`, `price`, `media_files`, `previews`,
        `reply_to_message_id`, ...); the base FanslyClient.send_message only
        takes `(group_id, text, *, in_reply_to)`, so without this override the
        extra kwargs raise TypeError -> 500.

        `text` is POSITIONAL-or-keyword, exactly as OFClient declares it. The
        HTTP send route passes `text=`, but the automation lane does not:
        `automations/scheduled_send.py` calls `send_message(fan_id, text, ...)`,
        so a keyword-only `text` here turned every deferred Fansly send into a
        TypeError at fire time. Match OFClient's signature and both lanes work.

        `chat_id` is the row id our list_chats emits — the Fansly groupId.
        Text, GIFs, vault media and PPV pricing all send; the rejections below
        are the cases Fansly genuinely cannot express, refused loudly rather
        than quietly downgraded into a send that only looks like it worked."""
        # A broadcast group id is freshly minted and is NOT in any DM listing,
        # so the usual fan-id -> group resolution would burn a
        # /messaging/groups scan and find nothing. Take it as-is.
        group_id = str(chat_id) if _broadcast else self._group_for_send(chat_id)
        media_files = list(media_files or [])
        check_sendable(media_files=media_files, price=price,
                       previews=previews, locked_text=locked_text)
        vault_ids = vault_ids_of(media_files)
        price_usd = float(price or 0)
        # A Fansly GIF is just a message whose content is the klipy URL: the
        # composer posts https://klipy.com/gifs/<slug> as plain content (no
        # attachment) and Fansly renders it. giphy_id carries that slug (or a
        # full url) from our gif_search/gif_trending picker.
        content = text or ""
        if giphy_id:
            # The picker sends the DIRECT gif url as giphy_id; recover the native
            # KLIPY page url for the wire so it matches Fansly's own client.
            gif_url = _klipy_wire_url(str(giphy_id))
            content = f"{content} {gif_url}".strip() if content else gif_url
        # Mint one accountMedia grant per attachment. This is the step that
        # makes vault media sendable at all — see _attach_media.
        attachments = []
        if vault_ids:
            fan_id = self._fan_id_for_group(group_id)
            if not fan_id:
                raise FanslyAPIError(
                    f"Can't attach media to group {group_id}: its recipient "
                    f"didn't resolve, and Fansly needs the accountId to "
                    f"whitelist who may view the attachment."
                )
            # OF's `previews` — the attachments shown FREE as the teaser on a
            # paid send. Only meaningful when a price is set; on a free send
            # every tile is already visible, so passing them would be noise.
            content_ids = self._attach_media(
                vault_ids, fan_id=fan_id, price_usd=price_usd,
                preview_ids=previews if price_usd > 0 else None,
            )
            # `pos` orders the tiles. Multi-attachment is PROVEN live (two
            # attachments, pos 0 + 1, both read back with their own grant and a
            # signed url) — no bundle (contentType 2) is needed for it, despite
            # bundles existing on the READ side. One grant per attachment.
            attachments = [
                {"messageId": None, "pos": i, "contentId": cid,
                 "contentType": self._MEDIA_CONTENT_TYPE}
                for i, cid in enumerate(content_ids)
            ]
        # Named explicitly rather than riding **_ignored: a broadcast that
        # silently took the plain /message path would post to the broadcast
        # group as an ordinary message.
        created = super().send_message(
            str(group_id),
            content,
            in_reply_to=str(reply_to_message_id) if reply_to_message_id else None,
            # The web client omits scheduledFor on GIF sends (captured HAR).
            scheduled_for=None if giphy_id else 0,
            attachments=attachments,
            broadcast=_broadcast,
        )
        # Return OF's message shape so the frontend's optimistic-send reconcile
        # works unchanged. It must describe what we ACTUALLY sent, because the
        # reconcile is `{...server}` — the response WINS over the optimistic
        # bubble (useChatMessages.mergeMedia). Two things therefore have to be
        # filled in, both of which Fansly's send response omits:
        #
        #   price — Fansly echoes the message at 0 and keeps the real figure on
        #     the grant, so returning it raw made a just-sent $101 PPV flip to
        #     "free" in the composer until the next refetch corrected it.
        #   media — the echo carries only contentIds. With an empty media[],
        #     mergeMedia's `!server.media.length` guard DISCARDS the optimistic
        #     tiles, so a sent image visibly vanished and then reappeared.
        #
        # Both are things we already know, so we answer with them rather than
        # letting the UI lie for one poll interval.
        if not isinstance(created, dict):
            # OBSERVED 2026-09-02 on the FIRST real broadcast: /message/broadcast
            # answers with a bare scalar in `response`, not the message object
            # /message echoes. The send had already fanned out on the socket
            # (a type-3 message in the broadcast group, then a type-2 copy per
            # recipient thread) when the reshape below AttributeError'd on it,
            # so a successful blast reported as a 500. Keep the scalar as the id
            # and fill the rest from what we sent.
            log.info("fansly: send response is a %s, not a message object: %r",
                     type(created).__name__, str(created)[:80])
            created = {"id": str(created) if created not in (None, "") else None,
                       "content": content, "groupId": str(group_id),
                       "senderId": str(self.session.account_id),
                       "createdAt": time.time()}
        return of_message(
            created,
            account_id=self.session.account_id,
            media=self._of_media_for_ids(vault_ids) if vault_ids else None,
            price=price_usd,
        )

    # -- fresh upload (vault) ---------------------------------------------
    # The flow the parity doc recorded as missing. Captured 2026-09-01 (see
    # capture/RECIPES.md "MEDIA UPLOAD"): four steps on a DIFFERENT host
    # (mediav2), the bytes going straight to presigned S3.
    _UPLOAD_TYPE = 1
    _UPLOAD_STATUS_READY = 6
    # The five formInputs the web client sends verbatim; their meaning is not
    # captured, so they are replayed byte-for-byte rather than interpreted.
    _UPLOAD_FORM_INPUTS = [
        {"type": 1001, "value": "false"}, {"type": 1002, "value": "false"},
        {"type": 1003, "value": ""}, {"type": 1004, "value": ""},
        {"type": 1005, "value": "\"\""},
    ]

    def upload_media(self, file_path: Any, *,
                     content_type: str | None = None,
                     check_dedupe: bool = True,
                     watermark_text: str | None = None,
                     poll_timeout_s: float = 120.0,
                     poll_interval_s: float = 1.0) -> dict:
        """Upload a file to the Fansly vault. OFClient-signature; returns the
        OF result shape with `send_with=[<vault mediaId>]` so the relay's
        upload route and the composer need no platform branch:

            {ready: True, vault_id: "<mediaId>", deduped: False,
             send_with: ["<mediaId>"], upload_id, size, filename, md5, etag,
             media: {...of_media readback...}, note}

        `check_dedupe` / `watermark_text` are accepted for signature parity and
        ignored: Fansly has no hash lookup (vault_media_lookup_hash is a
        declared no-op) and no server-side watermark field in this flow.

        Steps, each matching the capture field-for-field:
          1. POST mediav2 /media/upload/create  -> upload id + presigned S3
             url PER PART (`partSize` 20 MiB; a bigger file gets several parts).
          2. PUT each part's bytes to its presigned url. NO Fansly headers —
             the presign is the auth. Keep each response's ETag header.
          3. POST /media/upload/complete echoing every ETag back, QUOTES
             INCLUDED, by part index.
          4. Poll GET /media/upload/{id} until status 6 — `mediaId` is null
             until then. That mediaId (not the upload id) is the vault id.

        Fansly TRANSCODES (a PNG comes back as a JPEG), so the readback
        `media` block is what the vault actually holds, not what we sent.

        ⚠ Vault uploads are PERMANENT — there is no delete endpoint (see
        RECIPES "Vault deletion — NOT solved"). Callers that run repeatedly
        must upload once and reuse the vault id; never upload per run."""
        import hashlib
        import mimetypes
        import time as _time
        from pathlib import Path as _Path
        path = _Path(str(file_path))
        if not path.exists():
            raise FileNotFoundError(str(path))
        data = path.read_bytes()
        size = len(data)
        ct = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        md5_hex = hashlib.md5(data).hexdigest()

        # 1. create
        desc = self._request("POST", "/media/upload/create", base=MEDIA_BASE,
                             json_body={
                                 "fileSize": size,
                                 "mimeType": ct,
                                 "fileName": path.name,
                                 "uploadFormData": {
                                     "formInputs": list(self._UPLOAD_FORM_INPUTS)},
                             }) or {}
        upload_id = desc.get("id")
        parts = desc.get("parts") or []
        part_size = int(desc.get("partSize") or 0)
        if not upload_id or not parts or part_size <= 0:
            raise FanslyAPIError(
                f"/media/upload/create returned no upload id / parts for "
                f"{path.name}: {str(desc)[:200]}"
            )
        # A file bigger than partSize is split across parts by INDEX; the
        # server told us how many it expects, so a mismatch means we'd either
        # truncate the upload or leave a part unfilled — refuse, don't guess.
        expected = max(1, -(-size // part_size))
        if len(parts) != expected:
            raise FanslyAPIError(
                f"upload/create returned {len(parts)} part(s) for a {size}-byte "
                f"file at partSize {part_size}, expected {expected}"
            )

        # 2. PUT every part straight to S3 (presigned; Fansly auth would be
        # rejected there). The browser sends only `ngsw-bypass: true`.
        etags: list[tuple[int, str]] = []
        for part in sorted(parts, key=lambda p: int(p.get("index", 0))):
            idx = int(part.get("index", 0))
            chunk = data[idx * part_size:(idx + 1) * part_size]
            put = self.http.put(part["uploadUrl"], data=chunk,
                                headers={"ngsw-bypass": "true"},
                                timeout=max(float(self.timeout), 120.0))
            if not put.ok:
                raise FanslyAPIError(
                    f"S3 PUT part {idx} failed: {put.status_code} "
                    f"{put.text[:200]}", put)
            etag = put.headers.get("ETag") or put.headers.get("etag")
            if not etag:
                raise FanslyAPIError(
                    f"S3 PUT part {idx} returned no ETag; complete needs it")
            # S3 returns the ETag already double-quoted and Fansly wants it
            # echoed EXACTLY that way. Only add quotes if S3 dropped them.
            if not etag.startswith('"'):
                etag = f'"{etag}"'
            etags.append((idx, etag))

        # 3. complete
        self._request("POST", "/media/upload/complete", base=MEDIA_BASE,
                      json_body={
                          "id": str(upload_id),
                          "type": self._UPLOAD_TYPE,
                          "partSize": part_size,
                          "status": 0,
                          "parts": [{"index": i, "eTag": e} for i, e in etags],
                          "waitForComplete": 0,
                      })

        # 4. poll until the transcoder assigns the vault mediaId
        deadline = _time.monotonic() + poll_timeout_s
        state: dict = {}
        while True:
            state = self._request("GET", f"/media/upload/{upload_id}",
                                  base=MEDIA_BASE) or {}
            status = int(state.get("status") or 0)
            if status == self._UPLOAD_STATUS_READY and state.get("mediaId"):
                break
            if status > self._UPLOAD_STATUS_READY:
                raise FanslyAPIError(
                    f"upload {upload_id} ended in status {status} "
                    f"(no mediaId): {str(state)[:200]}")
            if _time.monotonic() >= deadline:
                raise FanslyAPIError(
                    f"upload {upload_id} still status {status} after "
                    f"{poll_timeout_s:.0f}s; not attaching an unfinished upload")
            _time.sleep(poll_interval_s)
        media_id = str(state["mediaId"])

        # Read the vault item back: Fansly may have transcoded it, so the
        # stored type/dimensions are the truth, not the bytes we sent. Live,
        # /media?ids= returned nothing for a second or two AFTER status 6, so
        # give it a few tries; an empty readback is reported, not fatal — the
        # mediaId is already the durable result.
        media: dict = {}
        for _ in range(4):
            try:
                media = self.vault_media_by_id(media_id) or {}
            except FanslyAPIError:
                media = {}
            if media:
                break
            _time.sleep(poll_interval_s)
        return {
            "ready": True,
            "vault_id": media_id,
            "deduped": False,
            "send_with": [media_id],
            "upload_id": str(upload_id),
            "upload_key": state.get("bucketKey"),
            "etag": etags[0][1].strip('"') if etags else None,
            "md5": md5_hex,
            "size": size,
            "filename": path.name,
            "stored_mime": state.get("mimeType"),
            "media": media,
            "existing": None,
            "note": f"uploaded — vault mediaId {media_id} (permanent; reuse it)",
        }

    # -- GIF search (OF routes GIFs through Giphy; Fansly proxies KLIPY) -----
    # Fansly's composer hits /gif-provider/{featured,search}; results map to OF's
    # GiphyResponse. Sending is handled in send_message (giphy_id -> klipy URL).

    def gif_trending(self, *, limit: int = 10, offset: int = 0) -> dict:
        """OF trending GIFs -> Fansly's KLIPY-backed featured feed."""
        resp = self._request("GET", "/gif-provider/featured")
        return _klipy_to_giphy(resp, offset=offset)

    def gif_search(self, query: str, *, limit: int = 10, offset: int = 0) -> dict:
        """OF GIF search -> Fansly's KLIPY-backed search."""
        resp = self._request("GET", "/gif-provider/search", params={"search": query})
        return _klipy_to_giphy(resp, offset=offset)

    # -- Notifications (real: GET /notifications) ---------------------------
    # Fansly's rows are bare: {id, accountId, type, correlationId,
    # correlationGroupId, acknowledgedAt, createdAt, metadata} — no embedded
    # account, no text. `type` is an int in the ONE namespace the capture
    # pinned down (`type == serviceId * 1000 + eventType`; see RECIPES
    # "Notification type codes"), and the label table below is lifted from the
    # client's own `this.filters=[...]`, so these are Fansly's names.
    #
    # OBSERVED live (2026-09-02, second account as counterparty): 3003 follow,
    # 5003 message like, 1002 post like, 1004 post reply — plus 15011
    # (Promotions) from before. Every other code is mapped from the client's
    # table, not from live data, and `metadata` was null on ALL rows including
    # the four new ones. So: map the code, pass `metadata` through untouched,
    # and never synthesize a field we haven't seen.

    # Fansly notification `type` -> OF's `type` string. The OF values are the
    # ones the bell's FILTERS and `notifSettings.mapOfTypeToKey` accept, so a
    # mapped row lands in the right tab; unmapped codes keep a "fansly:<n>"
    # type, which the frontend filters out rather than mislabels.
    _NOTIF_TYPE_MAP = {
        7001: "tip",
        2007: "purchases", 2008: "purchases",
        32007: "purchases", 45012: "purchases",
        3002: "subscribed", 3003: "subscribed",
        15006: "subscribed", 15016: "subscribed",
        1002: "favorited", 2002: "favorited", 5003: "favorited",
        1004: "commented", 1005: "commented",
    }
    # Human labels for the codes we can prove, for the row text. Kept separate
    # from the OF type because several codes collapse onto one OF bucket
    # (a follow and a subscribe are both "subscribed" to the bell) and the
    # text should still say which actually happened.
    _NOTIF_LABELS = {
        7001: "sent a tip",
        2007: "purchased media", 2008: "purchased media",
        32007: "purchased locked text", 45012: "purchased a stream ticket",
        # 3003 is OBSERVED (2026-09-02, twice): a live follow from a second
        # account emits a 3003 row. A live UNFOLLOW emits NO row at all — it
        # DELETES the 3003 row (feed went 7 -> 6; WS serviceId 9 type 3), so
        # "unfollowed you" is not a notification Fansly ever renders. 3002 has
        # never been observed; its label is a placeholder, not a claim.
        3003: "followed you", 3002: "follower update",
        15006: "subscribed", 15016: "subscribed", 15007: "subscription expired",
        15011: "promotion", 24001: "Fansly alert", 24002: "Fansly alert",
        1002: "liked your post", 2002: "liked your media",
        5003: "liked your message",
        1004: "replied to your post", 1005: "quoted your post",
    }

    # Which row field names the OTHER account, per code. The two ids on a row
    # are not "actor" and "object" in a fixed order — observed live:
    #   3003 follow:      correlationGroupId = follower,  correlationId = follow row
    #   1002 post like:   correlationId      = liker,     correlationGroupId = post
    #   5003 msg like:    correlationId = like row, correlationGroupId = MESSAGE (no actor)
    #   1004 post reply:  correlationId = reply post, correlationGroupId = root post (no actor)
    # Unlisted codes fall back to correlationGroupId (the pre-06 behaviour);
    # None means the row carries no account id and hydration is skipped rather
    # than looking up a post id as if it were a profile.
    _NOTIF_ACTOR_FIELD = {
        3002: "correlationGroupId", 3003: "correlationGroupId",
        1002: "correlationId", 2002: "correlationId",
        5003: None, 1004: None, 1005: None,
    }

    @classmethod
    def _notif_actor_id(cls, row: dict) -> str:
        try:
            code = int(row.get("type"))
        except (TypeError, ValueError):
            code = -1
        field = cls._NOTIF_ACTOR_FIELD.get(code, "correlationGroupId")
        return str(row.get(field) or "") if field else ""

    def _notification_rows(self) -> list:
        """Raw `GET /notifications` rows, newest first.

        `before`/`after` are the API's cursor pair; `0`/`0` is the unfiltered
        first page the web client itself requests. `type=` is sent EMPTY on
        purpose — Fansly's own client filters client-side, and a code passed
        here was never captured, so we do not guess a server-side filter."""
        resp = self._request(
            "GET", "/notifications",
            params={"before": "0", "after": "0", "type": ""},
        ) or {}
        rows = resp.get("notifications")
        return rows if isinstance(rows, list) else []

    def _of_notification(self, row: dict, users: dict) -> dict:
        """One Fansly notification row -> OF's notification item.

        `users` is the id -> of_user map hydrated by the caller from the
        row's actor field (see `_NOTIF_ACTOR_FIELD` — which of the two
        correlation ids is the other account differs per code) — the closest
        thing these rows carry to OF's embedded `user` object. It is passed in
        rather than fetched here so one page costs one batch /account call,
        not one per row."""
        code = row.get("type")
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = -1
        who = self._notif_actor_id(row)
        user = users.get(who) if who else None
        label = self._NOTIF_LABELS.get(code, "notification")
        name = (user or {}).get("name") or (user or {}).get("username") or ""
        # Money surfaces read the AMOUNT OUT OF THE TEXT (MoneyRail's
        # `amountFrom` regexes /\$…/ out of the rendered row, exactly as it
        # does for OF's "purchased your message for $25.22!"). So a priced row
        # has to SAY its price or the rail renders a sale with no figure —
        # which is what shipped until the live $3 capture. Appended only when
        # the row actually reports one; an unpriced row keeps its bare label.
        price = _notif_price_usd(row) if code in _PURCHASE_NOTIF_TYPES else 0.0
        if price:
            label = f"{label} for ${price:.2f}"
        return {
            "id": str(row.get("id") or ""),
            # An unmapped code stays namespaced rather than defaulting into a
            # real OF bucket — a mislabeled money event is worse than a hidden
            # one, and the raw code stays readable under `fanslyType`.
            "type": self._NOTIF_TYPE_MAP.get(code, f"fansly:{code}"),
            "text": f"{name} {label}".strip() if name else label,
            # Fansly stamps these in epoch SECONDS (both fields; verified on
            # the captured 15011 rows), and OF's bell parses an ISO string.
            "createdAt": _iso(row.get("createdAt")),
            "isRead": bool(row.get("acknowledgedAt")),
            "user": user or None,
            # Pass-through, never invented. Null on the social codes; the
            # money codes carry a JSON string (`accountMediaPrice`) — see
            # `_notif_price_usd`, which is what reads it.
            "metadata": row.get("metadata"),
            "fanslyType": code,
            # Structured twin of the price now in `text`, so a consumer that
            # wants the number doesn't have to re-parse prose. 0.0 on every
            # non-money row.
            "fanslyPriceUsd": price,
        }

    def purchases(self) -> list[dict]:
        """Every sale currently visible on this account's notification feed.

        THE public "what did fans pay for" read. Fansly gives creators no sales
        API — every such endpoint 404s — so a purchase notification is the only
        evidence a fan paid, and this method is where that fact lives. Callers
        get platform-neutral rows and need to know nothing about Fansly's
        notification codes, its 1/1000-dollar price unit or where in `metadata`
        the figure hides:

            [{"id": str,          # stable -> an idempotency key
              "fan_id": str,      # the BUYER's accountId
              "grant_id": str,    # the accountMedia bought ("" if absent)
              "price_usd": float, # dollars, already converted
              "created_at": int,  # epoch SECONDS
              "code": int}]       # the raw notification type, for logging

        It exists so service/fansly_revenue (which banks these into the money
        ledger) does not have to reach through four private names to do it.
        That coupling was real: the ledger and the notification bell read the
        same feed, and when they disagreed about which `metadata` key held the
        price, one surface rendered "$X" while the other banked nothing.

        Only codes PROVEN to mean a payment are returned (`_PURCHASE_NOTIF_TYPES`).
        A row whose price cannot be read is omitted rather than reported at
        0.0 — a free unlock and an unreadable price must not look alike to a
        caller that writes money.
        """
        out = []
        for row in self._notification_rows():
            if not isinstance(row, dict):
                continue
            try:
                code = int(row.get("type") or 0)
            except (TypeError, ValueError):
                continue
            if code not in _PURCHASE_NOTIF_TYPES:
                continue
            price = _notif_price_usd(row)
            if not price:
                # Dropping is right either way — fail-closed, so this can
                # understate but never fabricate. But WHY it dropped matters,
                # and only one of the two reasons is benign:
                #
                #   * a genuine free unlock reports its price as 0. Expected;
                #     stays quiet.
                #   * a row carrying NO price key at all is a metadata SHAPE
                #     CHANGE, i.e. real sales now being dropped. The feed is
                #     one page with no cursor, so those scroll off and are
                #     gone for good — this must raise its hand rather than
                #     quietly bleed revenue.
                if not any(k in _price_metadata(row) for k in _PRICE_METADATA_KEYS):
                    log.warning(
                        "purchase_row_unpriced account=%s notif=%s code=%s "
                        "meta=%.200r — no known price key; metadata shape may "
                        "have changed and SALES ARE BEING DROPPED",
                        getattr(getattr(self, "session", None), "account_id", "?"),
                        row.get("id"), code, row.get("metadata"))
                continue
            out.append({
                "id": str(row.get("id") or ""),
                "fan_id": str(row.get("correlationGroupId") or ""),
                "grant_id": str(row.get("correlationId") or ""),
                "price_usd": price,
                "created_at": row.get("createdAt"),
                "code": code,
            })
        return out

    def unbanked_purchase_codes(self) -> set[int]:
        """Codes this shim LABELS as purchases but has never proven.

        `45012` (stream ticket) is mapped for the notification bell, yet no
        live capture exists, so `purchases()` does not report it and no money
        is banked for it. Exposed so the revenue lane can LOG that gap instead
        of it sitting silent — money we knowingly do not count should be
        visible, and inventing a `kind` for an unverified code is worse.
        """
        return {code for code, of_type in self._NOTIF_TYPE_MAP.items()
                if of_type == "purchases" and code not in _PURCHASE_NOTIF_TYPES}

    def notifications(self, *, limit: int = 10, offset: int = 0,
                      type: str | None = None, **_ignored: Any) -> dict:
        """OF `/users/notifications` -> Fansly `GET /notifications`.

        `type` filters CLIENT-side on the mapped OF type, matching what
        Fansly's own web client does (its `this.filters` table is a local
        predicate over the same rows) — and matching OF's accepted values, so
        `send_welcome`'s `type="subscribed"` call selects follow/subscribe
        codes here without knowing it changed platform.

        Paging is client-side too: `/notifications` answers a whole page off a
        `before`/`after` cursor rather than an offset, so slicing the page we
        got is honest about what we fetched. A caller paging past the first
        page gets fewer rows, not wrong ones."""
        rows = self._notification_rows()
        # One batch profile fetch for the whole page — the rows carry only ids.
        who = {self._notif_actor_id(r) for r in rows if isinstance(r, dict)}
        who.discard("")
        users: dict = {}
        if who:
            try:
                users = self.list_users(sorted(who)) or {}
            except FanslyAPIError:
                # A profile lookup failing must not empty the feed: the rows
                # are the notification, the user is decoration.
                log.warning("notifications: profile hydration failed", exc_info=True)
        items = [self._of_notification(r, users) for r in rows if isinstance(r, dict)]
        if type:
            want = str(type).lower()
            items = [i for i in items if i["type"] == want]
        page = items[offset:offset + limit] if limit else items
        return {"list": page, "hasMore": offset + len(page) < len(items)}

    def notifications_count(self, **_ignored: Any) -> dict:
        """OF `/users/notifications/count` — the bell's per-category badge.

        Counted off the SAME rows the feed serves, so the badge can never
        disagree with the list under it. `/notifications/unack` is Fansly's
        own badge source and its shape is now known (2026-09-02, six unread
        rows present): `[{"type": <code>, "total": <n>}, ...]` — one entry per
        code with unacknowledged rows, `[]` only when everything is acked. The
        bell shows the sum. Deriving from the feed is kept because it is one
        fetch for both surfaces and can never disagree with the list; `unack`
        is a cheap cross-check if the badge is ever doubted.

        Only UNREAD (`acknowledgedAt` falsy) rows count, which is what a badge
        means. OF's key names are kept so the existing consumer is unchanged."""
        try:
            rows = self._notification_rows()
        except FanslyAPIError:
            log.warning("notifications_count: fetch failed", exc_info=True)
            rows = []
        buckets = {"subscribes": 0, "tips": 0, "comments": 0, "mentions": 0,
                   "hearts": 0, "gifts": 0, "all": 0}
        # OF's badge keys, from the type each row maps to. `mentions` and
        # `gifts` have no Fansly counterpart in the captured table and stay 0.
        by_of_type = {"subscribed": "subscribes", "tip": "tips",
                      "commented": "comments", "favorited": "hearts"}
        for row in rows:
            if not isinstance(row, dict) or row.get("acknowledgedAt"):
                continue
            try:
                code = int(row.get("type"))
            except (TypeError, ValueError):
                continue
            buckets["all"] += 1
            key = by_of_type.get(self._NOTIF_TYPE_MAP.get(code, ""))
            if key:
                buckets[key] += 1
        return buckets

    # -- Posts / timeline (real: GET /timelinenew/<accountId>) ---------------
    # Fansly's timeline returns {posts, accountMedia, accountMediaBundles,
    # accounts, tips} — the same media plumbing as /message. These back OF's
    # /posts routes, which previously AttributeError'd -> 500 on a Fansly
    # account (the relay's client has no OFClient inheritance).

    def list_posts(self, *, limit: int = 10, offset: int = 0, **_ignored: Any) -> dict:
        """OF `/posts` — our own posts."""
        return self.user_posts(self.session.account_id, limit=limit, offset=offset)

    def user_posts(self, user_id: str | int, *, limit: int = 10,
                   offset: int = 0, **_ignored: Any) -> dict:
        """OF `/users/{id}/posts` — one account's timeline.

        Fansly has no offset/limit on /timelinenew (it pages by `before`), so we
        slice locally and report hasMore honestly off the slice rather than
        pretending the server filtered."""
        tl = self._request("GET", f"/timelinenew/{user_id}") or {}
        page = of_posts_page(tl, limit=limit)
        rows = page["list"][offset:offset + limit] if limit else page["list"]
        return {"list": rows, "hasMore": offset + len(rows) < len(page["list"])}

    def get_post(self, post_id: int | str) -> dict:
        """OF `/posts/{id}` — one post with media. Fansly: GET /post?ids=."""
        resp = self._request("GET", "/post", params={"ids": str(post_id)}) or {}
        posts = resp.get("posts") if isinstance(resp, dict) else None
        if not posts:
            return {}
        am_by_id, bundle_by_id = _media_indexes(resp)
        return of_post(posts[0],
                       media=_resolve_attachments(posts[0], am_by_id, bundle_by_id))

    # -- Payouts (real: GET /payments/wallets) -------------------------------
    # Fansly exposes wallets, not OF's balances/requests split. Money MOVEMENT
    # stays ⛔; these are read-only balance reads that stop the payouts screen
    # 500ing, and they report what Fansly actually says rather than zeros.

    def payout_balances(self, **_ignored: Any) -> dict:
        """OF `/payouts/balances`. Fansly: wallets[]; sum their balances."""
        wallets = self._request("GET", "/payments/wallets") or []
        total = 0.0
        for w in wallets if isinstance(wallets, list) else []:
            try:
                total += float(w.get("balance") or 0)
            except (TypeError, ValueError):
                pass
        return {"total": total, "wallets": wallets if isinstance(wallets, list) else []}

    def payout_requests(self, *, limit: int = 10, offset: int = 0,
                        **_ignored: Any) -> dict:
        """OF `/payouts/requests` — payout history. Fansly exposes no request
        ledger we have identified, so this is an honest empty list, NOT a claim
        that no payouts exist. Capture-gated."""
        return {"list": [], "hasMore": False}

    # -- Subscribers / stories (real, endpoints from the web bundle) --------
    # `GET /subscribers` returns BOTH halves in one call:
    #   {stats: {totalActive, totalExpired, total}, subscriptions: [...]}
    # so it backs subscribers() AND the two count methods. Live-verified.
    #
    # subscribers_chart has no Fansly equivalent (totals only, no per-day
    # series) and lives with the other permanent no-ops below.

    def _subscribers_payload(self, *, before: str | None = None) -> dict:
        params: dict[str, Any] = {}
        if before:
            params["before"] = str(before)
        return self._request("GET", "/subscribers", params=params) or {}

    # Fansly subscription `status` (observed live 2026-09-02 on the socket:
    # 2 while the order settled, then 3 once active) — 3 is the steady active
    # state; anything else is treated as not-active for the "active" filter.
    _SUB_STATUS_ACTIVE = 3

    def _of_subscriber(self, row: dict, users: dict) -> dict:
        """One `/subscribers` row -> OF's subscriber fan object.

        Row shape (REAL, captured 2026-09-02 in capture/fixtures_subscribers.json):
        {id, historyId, subscriberId, subscriptionTierId, subscriptionTierName,
         subscriptionTierColor, planId, promoId, giftCodeId, paymentMethodId,
         status, price, renewPrice, renewCorrelationId, autoRenew, billingCycle,
         duration, renewDate, version, createdAt, updatedAt, endsAt, promo*}.
        Money is in CENTS, times in ms. There is NO embedded account, so the
        fan is hydrated from `subscriberId` (one batch /account per page) and
        the subscription facts ride on OF's names: `subscribedBy`,
        `subscribedByExpireDate`, `subscribePrice`, `currentSubscribePrice`."""
        sid = str(row.get("subscriberId") or "")
        fan = dict(users.get(sid) or {"id": sid, "name": "", "username": ""})
        # Money is Fansly's 1/1000-dollar unit (5000 == $5.00, verified against
        # the tier UI); times on THIS object are ms, unlike the seconds on
        # notification rows — same mixed-units trap as messages (RECIPES).
        renew = row.get("renewPrice") if row.get("renewPrice") is not None else row.get("price")
        ms = lambda v: _iso(float(v) / 1000.0) if v else None  # noqa: E731
        fan.update({
            "subscribedBy": row.get("status") == self._SUB_STATUS_ACTIVE,
            "subscribedByExpireDate": ms(row.get("endsAt")),
            "subscribedOnData": {"subscribeAt": ms(row.get("createdAt")),
                                 "expiredAt": ms(row.get("endsAt")),
                                 "renew": bool(row.get("autoRenew"))},
            "subscribePrice": (renew or 0) / 1000.0,
            "currentSubscribePrice": (row.get("price") or 0) / 1000.0,
            "fanslySubscription": {k: row.get(k) for k in (
                "id", "subscriptionTierId", "subscriptionTierName", "planId",
                "status", "giftCodeId", "autoRenew", "billingCycle", "duration")},
        })
        return fan

    def subscribers(self, *, type: str = "active", limit: int = 10,
                    offset: int = 0, online: bool = False,
                    **_ignored: Any) -> dict:
        """OF `/subscriptions/subscribers`. Fansly: GET /subscribers.

        Rows are reshaped through `_of_subscriber` — the shape was UNVERIFIED
        (this account had no subscribers) until a gift-code trial from a
        second account produced the first real row on 2026-09-02. `type`
        filters client-side on the subscription status ("active" == status 3,
        "expired" == anything else); `online=True` keeps only fans whose
        profile reports presence, which is what OF's `iter_online_subscribers`
        pages on. Fansly returns the whole roster in one page here."""
        payload = self._subscribers_payload()
        rows = [r for r in (payload.get("subscriptions") or []) if isinstance(r, dict)]
        want = str(type or "active").lower()
        if want == "active":
            rows = [r for r in rows if r.get("status") == self._SUB_STATUS_ACTIVE]
        elif want == "expired":
            rows = [r for r in rows if r.get("status") != self._SUB_STATUS_ACTIVE]
        ids = sorted({str(r.get("subscriberId") or "") for r in rows} - {""})
        users: dict = {}
        if ids:
            try:
                users = self.list_users(ids) or {}
            except FanslyAPIError:
                log.warning("subscribers: profile hydration failed", exc_info=True)
        fans = [self._of_subscriber(r, users) for r in rows]
        if online:
            fans = [f for f in fans if f.get("isOnline")]
        page = fans[offset:offset + limit] if limit else fans
        return {"list": page, "hasMore": offset + len(page) < len(fans)}

    def iter_online_subscribers(self, *, type: str = "active",
                                page_size: int = 20, max_fans: int = 1000,
                                **_ignored: Any) -> list[dict]:
        """OF's convenience walk; Fansly answers the roster in one page, so
        this is one `subscribers(online=True)` call capped at `max_fans`.
        Presence is `of_user.isOnline` (statusId == 1 on the profile — NOT the
        socket presence frame, see RECIPES)."""
        return self.subscribers(type=type, limit=max_fans, online=True)["list"]

    def subscription_counts(self, **_ignored: Any) -> dict:
        """OF `/subscriptions/count`. Fansly: the `stats` half of /subscribers."""
        stats = (self._subscribers_payload().get("stats") or {})
        return {
            "active": stats.get("totalActive") or 0,
            "expired": stats.get("totalExpired") or 0,
            "all": stats.get("total") or 0,
        }

    def subscription_counts_all(self, **_ignored: Any) -> dict:
        """OF `/subscriptions/count/all` — the fuller breakdown. Fansly reports
        only active/expired/total, so we return those under OF's key names and
        omit the ones it doesn't know (blocked/muted/...) rather than sending
        zeros that would read as 'we checked and there are none'."""
        return self.subscription_counts()

    def stories_items(self, *_args: Any, **_ignored: Any) -> dict:
        """OF `/stories/items`. Fansly: /stories + /mediastories for our own
        account. Returns OF's list shape; live-verified (empty on this account,
        which has no stories)."""
        acct = str(self.session.account_id)
        out = []
        for path in ("/stories", "/mediastories"):
            try:
                r = self._request("GET", path, params={"accountId": acct})
            except FanslyAPIError:
                continue
            if isinstance(r, list):
                out.extend(r)
        return {"list": out, "hasMore": False}

    # -- Vault ---------------------------------------------------------
    # The whole vault surface (album folders, their media, and the folder
    # write side) lives in `fansly_vault.VaultMixin`, which this class
    # inherits. It needs only `_request` + `session`, so it splits off
    # cleanly and is testable against a fake client with no network.

    def chat_media(self, chat_id: str | int | None = None, **_ignored: Any) -> dict:
        return {"list": [], "hasMore": False}

    # OF's get_pinned_messages returns the ROWS, not an envelope (see
    # of_client) and _pins iterates them — a {"list": []} here would hand
    # _pins the string "list" as a pinned message.
    @fansly_unsupported("Fansly has no message pinning", returns=list)
    def get_pinned_messages(self, *_a: Any, **_k: Any) -> list: ...

    def schedules(self, **_ignored: Any) -> dict:
        return {"list": [], "hasMore": False}

    def scheduled_messages(self, **_ignored: Any) -> dict:
        return {"list": [], "hasMore": False}

    # -- read state ---------------------------------------------------------
    # The relay route already clears our LOCAL unread (what drives the inbox
    # dot) before calling these, so they only need to sync read-state to Fansly
    # — and Fansly's mark-read endpoint isn't identified yet (every guessed path
    # 404s: /message/markread, /messaging/groups/markread, /messaging/groups/
    # {id}/read). So these are graceful no-ops: they stop the AttributeError→500
    # the OF route hit, keep the UI correct via the local clear, and simply skip
    # the platform-side sync until a capture reveals the real call. (Opening a
    # thread may already mark it read Fansly-side via the message fetch.)
    # How many messages one chat-open is willing to ack. A thread with a
    # genuinely huge unread backlog acks its most recent page and the rest
    # clears on the next open — bounded work beats an unbounded body.
    _ACK_PAGE = 50

    def _unread_message_ids(self, group_id: str) -> list[str]:
        """Ids of messages in `group_id` that WE have not read yet.

        The test is per-message and lives on the message's `interactions[]`:
        our own row with a falsy `readAt`. Sender is checked too — our own
        sends carry an interaction for the fan, not for us, and acking our own
        message is meaningless. Newest-first, as `/message` returns them."""
        me = str(self.session.account_id)
        try:
            payload = self._request(
                "GET", "/message",
                params={"groupId": str(group_id), "limit": str(self._ACK_PAGE)},
            ) or {}
        except FanslyAPIError:
            log.warning("unread scan failed for group %s", group_id, exc_info=True)
            return []
        out: list[str] = []
        for msg in (payload.get("messages") or []):
            if not isinstance(msg, dict) or str(msg.get("senderId") or "") == me:
                continue
            unread = any(
                str(i.get("userId") or "") == me and not i.get("readAt")
                for i in (msg.get("interactions") or [])
                if isinstance(i, dict)
            )
            if unread and msg.get("id"):
                out.append(str(msg["id"]))
        return out

    def mark_chat_read(self, chat_id: str | int) -> dict:
        """OF `POST /chats/{id}/mark-as-read` -> Fansly `POST /message/ack`.

        Captured live (messageread.har): opening an unread thread in Fansly web
        posts ``{"messageIds": ["<id>"], "type": 2}`` and answers
        ``{"success": true}``.

        We ack EVERY unread message in the thread, not just the membership's
        ``lastUnreadMessageId``. Acking that one id alone looks right and is
        not: measured live on a thread with ``unreadCount: 2``, acking the
        newest id returned success and left ``unreadCount`` at 2, while acking
        both ids took it to 0. Fansly counts unread per MESSAGE, so a pointer
        ack clears the pointer, not the count — and any thread where the fan
        sent two messages before we opened it would stay blue forever.

        This used to be a STUB that returned ``{"success": True}`` without
        calling Fansly at all. The lie was invisible but total: the UI moved the
        roster badge blue -> orange on open, Fansly's ``unreadCount`` never
        changed, and the next 60s poll put the blue straight back. Opening a
        chat simply did not stick.

        Note we do NOT ack on send — Fansly's web client doesn't either, and it
        doesn't need to: a reply makes ``lastMessageId`` newer than
        ``lastUnreadMessageId``, which `of_chat_row` already reads as "cleared".

        Best-effort by contract: the caller (server.of_mark_chat_read) treats
        this as fire-and-forget, so a thread with nothing unread is a no-op
        rather than an error."""
        group_id = self._as_group_id(chat_id)
        message_ids = self._unread_message_ids(group_id)
        if not message_ids:
            return {"success": True}   # nothing unread — nothing to ack
        try:
            self._request(
                "POST", "/message/ack",
                json_body={"messageIds": message_ids, "type": 2},
            )
        except FanslyAPIError as e:
            # A STALE unread pointer is real and unfixable from here: seen live
            # on a thread whose lastUnreadMessageId names a message Fansly will
            # neither return from /message nor accept here (400 code=99). That
            # chat stays blue in Fansly's own client too, so matching it is
            # correct parity — but it must not turn an ordinary chat-open into a
            # 502. The route is fire-and-forget; report the miss and move on.
            log.warning("fansly ack failed for group %s (msgs %s): %s",
                        group_id, message_ids, e)
            return {"success": False, "reason": "ack_rejected"}
        return {"success": True}

    def mark_chat_unread(self, chat_id: str | int) -> dict:
        # Deliberate no-op, now with evidence (2026-09-02). Fansly web's
        # "Mark as Unread" sends `POST /message/ack {messageIds:[<oldest
        # unread-able fan message>], type: 4}` and flips the envelope badge to
        # 1 — CLIENT-SIDE ONLY. A reload clears the badge, `/messaging/groups`
        # keeps `unreadCount: 0`, and `/message/unread.total` stays 0; a type-4
        # ack sent from this shim changes nothing observable either. There is
        # no server-side unread state to set, so success-without-effect is the
        # honest answer (OF's semantics can't be reproduced here).
        return {"success": True}

    # Bound on how many THREADS one mark-all may ack. Unread is per-message, so
    # each unread thread costs one /message scan to collect its ids; the ack
    # itself stays a single POST (the captured endpoint takes a list). The cap
    # bounds both: a bug upstream (say, unreadCount misread on every group)
    # can mis-ack at most this many threads, not the entire inbox.
    _MARK_ALL_CAP = 50

    def mark_all_chats_read(self, **_ignored: Any) -> dict:
        """OF `POST /chats/mark-as-read` -> one batched Fansly `/message/ack`.

        Same mechanics as `mark_chat_read` — every unread message id, not the
        `lastUnreadMessageId` pointer (see that method for why the pointer
        alone leaves `unreadCount` standing) — but the group list is read once
        instead of once per thread, and every id collected goes out in a single
        POST. A thread whose scan comes back empty is skipped, not failed.
        Fire-and-forget by the same contract as the single-thread path."""
        groups = self._request(
            "GET", "/messaging/groups",
            params={"sortOrder": "1", "flags": "0", "subscriptionTierId": "",
                    "listIds": "", "search": "", "limit": "100", "offset": "0"},
        )
        unread_groups = [
            str(m.get("groupId"))
            for m in (groups.get("data") or [])
            if isinstance(m, dict) and m.get("groupId")
            and int(m.get("unreadCount") or 0)
        ][: self._MARK_ALL_CAP]
        message_ids: list[str] = []
        for gid in unread_groups:
            message_ids.extend(self._unread_message_ids(gid))
        if not message_ids:
            return {"success": True}   # nothing unread — nothing to ack
        try:
            self._request(
                "POST", "/message/ack",
                json_body={"messageIds": message_ids, "type": 2},
            )
        except FanslyAPIError as e:
            log.warning("fansly mark-all ack failed (%d ids across %d threads): %s",
                        len(message_ids), len(unread_groups), e)
            return {"success": False, "reason": "ack_rejected"}
        return {"success": True}

    # -- earnings (money — out of scope on Fansly) --------------------------
    def transactions(self, *, limit: int = 20, offset: int = 0,
                     type: str | None = None, start: str | None = None,
                     end: str | None = None, **_ignored: Any) -> dict:
        """OF's earnings ledger (drives FanDrawer's last-purchase/spend chip).
        Money is out of scope on the Fansly path (no tips/PPV/payout surfacing),
        so return an empty ledger in OF's `{list, hasMore}` shape: this is the
        SCOPED-OUT result — not a false-empty hiding data — and it stops the
        AttributeError→500 the route hit when the drawer polled it."""
        return {"list": [], "hasMore": False}

    # -- helpers ------------------------------------------------------------

    @property
    def _known_group_ids(self) -> set[str]:
        """Per-instance cache of groupIds we've emitted from list_chats. Lazily
        created in the instance dict so we never share one set across accounts
        (a class-level ``set()`` default would) and never touch
        FanslyClient.__init__."""
        cache = self.__dict__.get("_group_id_cache")
        if cache is None:
            cache = set()
            self.__dict__["_group_id_cache"] = cache
        return cache

    @property
    def _fan_to_group(self) -> dict[str, str]:
        """Per-instance fan-accountId -> groupId map, filled by list_chats."""
        cache = self.__dict__.get("_fan_group_map")
        if cache is None:
            cache = {}
            self.__dict__["_fan_group_map"] = cache
        return cache

    @property
    def _group_to_fan(self) -> dict[str, str]:
        """Per-instance groupId -> fan-accountId map — the inverse of
        ``_fan_to_group``, filled by the same list_chats pass."""
        cache = self.__dict__.get("_group_fan_map")
        if cache is None:
            cache = {}
            self.__dict__["_group_fan_map"] = cache
        return cache

    def _purchases_by_fan(self) -> dict:
        """buyer accountId -> frozenset of accountMedia grant ids they bought.

        The creator-side sales endpoints all 404 (/account/media/purchases,
        /purchases, /statements, /account/sales, …), so `/notifications` is the
        only place a purchase is visible to us: a fan paying emits a
        `2007`/`2008` row whose `correlationId` is the grant they bought and
        whose `correlationGroupId` is the buyer. See of_message's isOpened note
        for the live confirmation.

        Keyed by BUYER on purpose: the feed is account-wide, so a flat set of
        grant ids would unlock fan B's bubble because fan A bought the same
        vault item.

        The whole feed in ONE pass, cached as one object rather than per fan:
        `/notifications` is account-wide, so a per-fan cache re-fetched and
        re-scanned the SAME page for every thread — opening a 50-row inbox
        meant 50 identical requests that each threw away 49/50ths of the
        answer. Fans with no purchases are simply absent from the map, which
        is why callers read it with a `frozenset()` default.

        Failures reuse the last good map (a feed hiccup must never flip a paid
        message back to locked mid-session) and fall back to empty, never to a
        false unlock.

        DURABILITY — read this before trusting the unlock. `/notifications` is
        ONE page and account-wide, so an old purchase eventually scrolls off
        and this map stops reporting it. What makes the unlock survive that is
        NOT this cache: automation_executor (scrape) and event_transcoder (live
        WS frame) both persist `isOpened` into `messages.is_paid` via
        `db.models.is_paid_ratchet`, so True never reverts and a sale observed
        once while it was still on the feed stays recorded. Without that ratchet this feature would silently re-lock paid
        messages and under-report revenue (funnel_stats_api sums over is_paid;
        ownership.py gates on it) — the two must be kept in step.

        The TTL is arbitrary, tuned to a manual refresh cadence so a purchase
        appears when the chatter presses ↻ rather than after a restart; 30 or
        120 would serve equally well.

        NOT the canonical purchase reader. service/purchase_notifications.py
        already owns "read the purchases feed, flip messages.is_paid durably",
        with a 20-page backfill and idempotent writes — but it is OF-only
        today: its `_fetch_feed` calls `client.get(...)`, a method this shim
        does not have, inside a bare `except`, so it silently no-ops for every
        Fansly account. Wiring the shim into it would SUPERSEDE this read-path
        cache and give durable, paginated unlock; until then this is the only
        thing that answers "did he pay" on Fansly. Tracked as follow-up."""
        lock = self.__dict__.setdefault("_purchase_lock", threading.Lock())
        # Single-flight: this client is a process-wide singleton per account
        # (service/fansly_backend.get_client) served from a 64-thread pool, so
        # without the lock K concurrent inbox reads on a cold cache would fire
        # K identical /notifications requests — the very stampede this cache
        # exists to prevent. The lock is held across the fetch so the losers
        # wait and then read the winner's result.
        with lock:
            cached = getattr(self, "_purchase_cache", None)
            if cached and (time.time() - cached[0]) < _PURCHASE_TTL_S:
                return cached[1]
            try:
                rows = self._notification_rows()
            except (FanslyAPIError, requests.RequestException):
                # Narrow on purpose: a feed hiccup reuses the last good map, but
                # a TypeError/AttributeError from a shape change must NOT be
                # swallowed into "nothing purchased" — that would render every
                # PPV locked forever with no log line to explain it.
                log.warning("purchase_feed_failed account=%s",
                            self.session.account_id, exc_info=True)
                return cached[1] if cached else {}
            by_fan: dict = {}
            for row in rows:
                try:
                    rtype = int(row.get("type") or 0)
                except (TypeError, ValueError):
                    continue
                if rtype not in _PURCHASE_NOTIF_TYPES:
                    continue
                buyer = str(row.get("correlationGroupId") or "")
                bought = str(row.get("correlationId") or "")
                if buyer and bought:
                    by_fan.setdefault(buyer, set()).add(bought)
            frozen = {k: frozenset(v) for k, v in by_fan.items()}
            self._purchase_cache = (time.time(), frozen)
            return frozen

    def _fan_id_for_group(self, group_id: str | int) -> str | None:
        """The other party's accountId for a DM group.

        Needed only by the media path: POST /account/media whitelists exactly
        who may view the attachment, and it wants an accountId while the send
        API speaks groupIds. Warm after any list_chats; an automation that
        sends without ever listing the inbox pays for one bounded scan of
        /messaging/groups here, mirroring find_group_with in the other
        direction. Returns None rather than raising so the caller decides."""
        gid = str(group_id)
        hit = self._group_to_fan.get(gid)
        if hit:
            return hit
        offset = 0
        while offset < 200:
            rows = (self.groups(limit=20, offset=offset) or {}).get("data") or []
            for row in rows:
                fan, grp = row.get("partnerAccountId"), row.get("groupId")
                if fan and grp:
                    self._group_to_fan[str(grp)] = str(fan)
                    self._fan_to_group[str(fan)] = str(grp)
            if gid in self._group_to_fan:
                return self._group_to_fan[gid]
            if len(rows) < 20:
                return None
            offset += 20
        return None

    # -----------------------------------------------------------------
    # Automation surface — the methods the 81 plugins actually call.
    # Every request body below is a CAPTURED shape (capture/RECIPES.md), not
    # an extrapolation. The recurring trap in this API is that sibling
    # resources do NOT share conventions: three different delete shapes, and
    # list add/remove take two different payloads. Derive each; never
    # generalise one from another.
    # -----------------------------------------------------------------

    def follow_user(self, user_id: str | int, **_ignored: Any) -> dict:
        """POST /account/{id}/followers — no body.

        A 422 here is ACCOUNT POLICY, not a malformed request: code 2 is a
        reserved/system account, code 5 a creator who no longer offers a free
        follow. Both are normal outcomes to report, so they return a clean
        skip rather than raising — a retry loop would never succeed and
        auto_follow would burn its budget on it."""
        try:
            self._request("POST", f"/account/{user_id}/followers")
        except FanslyAPIError as exc:
            if "422" in str(exc):
                log.info("fansly: %s does not accept a free follow (%s)",
                         user_id, exc)
                return {"success": False, "skipped": "not-followable"}
            raise
        return {"success": True}

    def unfollow_user(self, user_id: str | int, **_ignored: Any) -> dict:
        """POST /account/{id}/followers/remove — no body."""
        self._request("POST", f"/account/{user_id}/followers/remove")
        return {"success": True}

    # Fansly has no plain "like": the heart+plus control opens a 7-emoji
    # reaction picker and picking one IS the like. Heart is type 1 — CAPTURED.
    # Heart is the 4th emoji in the picker, so the type is NOT the picker
    # index and the other six codes are unknown. Do not guess them.
    _REACTION_HEART = 1

    def like_message(self, message_id: str | int, *_a: Any,
                     **_ignored: Any) -> dict:
        """POST /message/like {messageId, type}. Only the COUNTERPARTY's
        message can be reacted to; liking our own is refused by the API, so it
        returns a skip rather than taking down the calling automation."""
        try:
            self._request("POST", "/message/like", json_body={
                "messageId": str(message_id), "type": self._REACTION_HEART})
        except FanslyAPIError as exc:
            log.info("fansly: could not react to message %s (%s)",
                     message_id, exc)
            return {"success": False, "skipped": "not-likeable"}
        return {"success": True}

    def unsend_message(self, message_id: str | int, *_a: Any,
                       **_ignored: Any) -> dict:
        """POST /message/delete with the id in the BODY.

        Contrast delete_post, where the id is in the PATH — see the class
        comment. The UI's confirm dialog is client-side; one call does it."""
        self._request("POST", "/message/delete",
                      json_body={"messageId": str(message_id)})
        return {"success": True}

    def _main_wall_id(self) -> str | None:
        """The profile wall a post lands on. `wallIds` is REQUIRED by
        POST /post, and it is not a constant — it is per-account, so it is
        read from the live account rather than baked in."""
        if self.__dict__.get("_wall_id"):
            return self.__dict__["_wall_id"]
        account = self._request("GET", "/account/me").get("account") or {}
        walls = account.get("walls") or []
        wall = next((w for w in walls if w.get("mainWall")), None) \
            or next((w for w in walls if w.get("defaultWall")), None) \
            or (walls[0] if walls else None)
        wid = str(wall["id"]) if wall and wall.get("id") else None
        self.__dict__["_wall_id"] = wid
        return wid

    def create_post(self, text: str = "", *, media_files: Any = None,
                    scheduled_for: Any = None, **_ignored: Any) -> dict:
        """POST /post. `wallIds` is required.

        `scheduledFor` is MILLISECONDS (verified by readback), while the
        message lane's `scheduledFor` is seconds — a units mismatch that would
        publish a scheduled post IMMEDIATELY if seconds were passed here, since
        a seconds value reads as 1970 and any past time posts now. So the
        conversion is explicit and the parameter is documented as epoch
        seconds, matching OF's convention on the caller's side."""
        wall_id = self._main_wall_id()
        if not wall_id:
            raise FanslyAPIError(
                "Cannot create a Fansly post: the account exposes no wall to "
                "post to (POST /post requires wallIds).")
        media_files = list(media_files or [])
        if media_files:
            # Posts attach the same accountMedia grants messages do, but the
            # whitelist/pricing model for a WALL post is not captured, and
            # guessing it risks publishing paid media free to everyone.
            raise FanslyAPIError(
                "Attaching media to a Fansly post is not wired yet — the "
                "post-side grant shape is uncaptured. Post text only.")
        body: dict[str, Any] = {
            "content": text or "",
            "fypFlags": 0,
            "inReplyTo": None,
            "quotedPostId": None,
            "attachments": [],
            "scheduledFor": int(float(scheduled_for) * 1000) if scheduled_for else 0,
            "expiresAt": 0,
            "postReplyPermissionFlags": [],
            "pinned": 0,
            "wallIds": [wall_id],
            "pinWallIds": [],
        }
        # `_request` already unwraps Fansly's {success, response} envelope, so
        # the post object IS the top level here. Reaching for a "response" key
        # again silently yields {} — the post gets created and the caller is
        # handed id=None, i.e. a live post nobody can delete. Cost real state.
        post = self._request("POST", "/post", json_body=body) or {}
        post = post if isinstance(post, dict) else {}
        # A SCHEDULED create omits the id from the response (it is not a post
        # yet); it surfaces in the queue as `postId`. Report what we have
        # rather than inventing an id the caller would then fail to delete.
        pid = post.get("id")
        return {"success": True, "id": str(pid) if pid else None,
                "scheduled": bool(body["scheduledFor"])}

    def delete_post(self, post_id: str | int, **_ignored: Any) -> dict:
        """POST /post/{id}/delete — id in the PATH, NO body.

        On a SCHEDULED post this 422s (code 99: a queued post is not a post) —
        use cancel_scheduled for that lane."""
        self._request("POST", f"/post/{post_id}/delete")
        return {"success": True}

    def cancel_scheduled(self, post_id: str | int, **_ignored: Any) -> dict:
        """POST /post/scheduled/{postId}/cancel — no body. The third distinct
        delete/cancel shape in this API; see the class comment."""
        self._request("POST", f"/post/scheduled/{post_id}/cancel")
        return {"success": True}

    def add_user_to_list(self, list_id: str | int, user_id: str | int,
                         **_ignored: Any) -> dict:
        """POST /lists/commands, type 1 = add.

        Add and remove take DIFFERENT payload shapes — add nests a singular
        `listItem{id,listId}`, remove takes `listId` beside a plural
        `itemIds[]`. Sending the add shape for a remove is accepted and
        silently does nothing, so the two are written out separately here
        rather than sharing a builder."""
        self._request("POST", "/lists/commands", json_body={"listCommands": [
            {"type": 1, "listItem": {"id": str(user_id),
                                     "listId": str(list_id)}}]})
        return {"success": True}

    def remove_user_from_list(self, list_id: str | int, user_id: str | int,
                              **_ignored: Any) -> dict:
        """POST /lists/commands, type 2 = remove — the plural shape."""
        self._request("POST", "/lists/commands", json_body={"listCommands": [
            {"type": 2, "listId": str(list_id),
             "itemIds": [str(user_id)]}]})
        return {"success": True}

    def create_list(self, name: str, description: str = "",
                    **_ignored: Any) -> dict:
        """POST /lists {label, description}."""
        row = self._request("POST", "/lists", json_body={
            "label": name, "description": description or ""}) or {}
        row = row if isinstance(row, dict) else {}
        return {"id": str(row["id"]) if row.get("id") else None,
                "name": row.get("label") or name}

    def get_lists(self, **_ignored: Any) -> dict:
        """GET /lists/account returns ONLY user-created lists. The Blocked /
        Muted / VIP / DM-Allow rows in the UI are client-side built-ins, so an
        empty result does NOT mean the account has no lists."""
        rows = self._request("GET", "/lists/account",
                             params={"itemId": ""}) or []
        rows = rows if isinstance(rows, list) else []
        return {"list": [{"id": str(r.get("id")), "name": r.get("label") or ""}
                         for r in rows if isinstance(r, dict) and r.get("id")],
                "hasMore": False}

    # -----------------------------------------------------------------
    # Permanent no-ops — features Fansly does not have. See the
    # fansly_unsupported docstring; these are intentional, not unfinished.
    # -----------------------------------------------------------------

    # Verified absent: the message hover menu is reply/heart/react/delete and
    # the thread menu has no pin. This was an ACTIVE CRASH — _pins calls
    # pin_message inside ai_chatter, so the AttributeError took down the whole
    # chatter run, not just the pinning.
    @fansly_unsupported("Fansly has no message pinning",
                        returns=lambda: {"success": True})
    def pin_message(self, *_a: Any, **_k: Any) -> dict: ...

    @fansly_unsupported("Fansly has no message pinning",
                        returns=lambda: {"success": True})
    def unpin_message(self, *_a: Any, **_k: Any) -> dict: ...

    # Vault uploads are PERMANENT — a real operational constraint, not a gap in
    # our client. /vault/albums/media/delete only unlinks from an album, and
    # /account/media/{id}/delete wants a grant id, not a vault mediaId.
    @fansly_unsupported("Fansly vault media cannot be deleted",
                        returns=lambda: {"success": False})
    def hide_vault_media(self, *_a: Any, **_k: Any) -> dict: ...

    @fansly_unsupported("Fansly has no vault hash lookup", returns=None)
    def vault_media_lookup_hash(self, *_a: Any, **_k: Any) -> None: ...

    # Stories appear mobile-app-only; the web client has no create path. Since
    # no story can be created on a Fansly account, delete_story should never
    # find a target — it is cheap insurance for a target arriving via shared
    # state, and it completes the capability contract.
    @fansly_unsupported("Fansly stories cannot be created from the web API",
                        returns=lambda: {"success": False})
    def post_story_from_url(self, *_a: Any, **_k: Any) -> dict: ...

    @fansly_unsupported("Fansly stories are not creatable, so none exist",
                        returns=lambda: {"success": False})
    def delete_story(self, *_a: Any, **_k: Any) -> dict: ...

    # Analytics/read endpoints with no Fansly equivalent. Never FAKED — a
    # synthesised daily series would be invented analytics, worse than a
    # missing chart — but not ABSENT either: server.py routes call these
    # unconditionally, so a missing attribute is an AttributeError 500 on the
    # Statistics/Stories/Bookmarks screens rather than a graceful degrade.
    # Each returns OF's documented empty shape, which the callers already
    # handle (useStats' useOfNewFans sums an empty `subscribes[]` to 0).

    @fansly_unsupported(
        "Fansly reports subscriber TOTALS only, with no per-day series; "
        "synthesising one would be invented analytics",
        returns=lambda: {"subscribers": 0, "subscribes": [], "earnings": [],
                         "total": 0, "delta": 0})
    def subscribers_chart(self, *_a: Any, **_k: Any) -> dict: ...

    @fansly_unsupported("OF's stories map is GEO analytics; Fansly exposes no geo",
                        returns=dict)
    def stories_map(self, *_a: Any, **_k: Any) -> dict: ...

    @fansly_unsupported("no Fansly on-this-day/memories endpoint exists",
                        returns=lambda: {"list": [], "hasMore": False})
    def posts_on_this_day(self, *_a: Any, **_k: Any) -> dict: ...

    @fansly_unsupported("no Fansly post-bookmarks endpoint exists", returns=list)
    def bookmarked_posts(self, *_a: Any, **_k: Any) -> list: ...

    # -----------------------------------------------------------------
    # Blocked on account state — NOT permanent no-ops. Each needs a capture
    # this test account cannot produce (0 subscribers, no payment method).
    # They return an empty shape so an automation degrades to a clean skip
    # instead of AttributeError-ing mid-run; replace with a real
    # implementation once the capture exists.
    # -----------------------------------------------------------------

    # Fansly calls a mass message a BROADCAST. The whole flow is recovered in
    # capture/BUNDLE_DERIVED.md — read that before touching this.
    #
    # The shape is simpler than it looked: a broadcast is the SAME message body
    # a DM uses, POSTed to /message/broadcast, with groupId pointing at a
    # type-3 "broadcast group" that encodes the audience. So minting the group
    # is the only new step, and media/PPV attach unchanged.
    #
    # NOT SHIPPED ON, AND THE GATE IS DELIBERATE. Everything below is read from
    # the JS bundle and has never touched a server. Four unknowns, any of which
    # makes an unattended first run a bad idea:
    #   1. no live exercise — the server may require fields the client always
    #      happens to send, or reject an empty audience;
    #   2. `groupFlags = 32` is the modal's initial value, meaning unknown;
    #   3. include vs exclude differ by a trailing +1 on the recipient type
    #      (30000 vs 30001) — inverting it blasts exactly the audience the
    #      operator meant to suppress;
    #   4. idempotency is unknown, so a careless retry may re-send to everyone.
    # A mass send is unrecallable: unsend_message deletes one message by id, and
    # nothing here enumerates what a broadcast fanned out to.
    _BROADCAST_GROUP_TYPE = 3
    _BROADCAST_GROUP_FLAGS = 32          # modal default; meaning unknown
    _LISTS_SERVICE_ID = 30               # ServiceIds.ListsService
    RECIPIENT_INCLUDE_LIST = _LISTS_SERVICE_ID * 1000        # 30000
    RECIPIENT_EXCLUDE_LIST = _LISTS_SERVICE_ID * 1000 + 1    # 30001

    def _broadcast_group(self, *, include_lists: list, exclude_lists: list,
                         tier_id: Any = None) -> str:
        """Mint the type-3 group a broadcast is addressed to (POST /group).

        Bundle-derived; see the block above. Separate from send_mass_message so
        the audience can be built and INSPECTED before anything is sent."""
        recipients = [{"recipientId": str(i),
                       "type": self.RECIPIENT_INCLUDE_LIST}
                      for i in include_lists]
        recipients += [{"recipientId": str(i),
                        "type": self.RECIPIENT_EXCLUDE_LIST}
                       for i in exclude_lists]
        if not recipients:
            raise FanslyAPIError(
                "A broadcast needs at least one recipient list — a group with "
                "no audience is not a safe thing to guess the meaning of.")
        metadata = ""
        if tier_id:
            # Doubly-encoded, same nesting style as the PPV price metadata.
            metadata = _json.dumps(
                {"4": _json.dumps({"subscriptionTierId": str(tier_id)},
                                  separators=(",", ":"))},
                separators=(",", ":"))
        body = {
            "type": self._BROADCAST_GROUP_TYPE,
            "groupFlags": self._BROADCAST_GROUP_FLAGS,
            "groupFlagsMetadata": metadata,
            "users": [{"userId": str(self.session.account_id),
                       "permissionFlags": 65535}],
            "recipients": recipients,
            "lastMessage": None,
            "userSettings": None,
        }
        group = self._request("POST", "/group", json_body=body) or {}
        gid = group.get("id") if isinstance(group, dict) else None
        if not gid:
            raise FanslyAPIError(
                "POST /group accepted the broadcast group but returned no id.")
        return str(gid)

    def send_mass_message(self, text: str = "", *, media_files: Any = None,
                          price: Any = 0, include_lists: Any = None,
                          exclude_lists: Any = None, tier_id: Any = None,
                          confirm_live_broadcast: bool = False,
                          **_ignored: Any) -> dict:
        """Send a Fansly broadcast. GATED — see the block above.

        `confirm_live_broadcast=True` is required to actually send. That flag is
        not bureaucracy: this is the one call in the shim that reaches an
        unbounded audience in a single unrecallable request, built entirely from
        an unverified bundle reading. The default path returns a clean skip so
        an automation that calls this degrades instead of blasting.

        The FIRST real send should be to a list containing one account you
        control, with the request logged, so the derived shape gets confirmed
        against a server before anyone else receives anything."""
        include_lists = [i for i in (include_lists or []) if i]
        exclude_lists = [i for i in (exclude_lists or []) if i]
        if not confirm_live_broadcast:
            log.warning(
                "fansly: send_mass_message called without "
                "confirm_live_broadcast — refusing to broadcast. The wire "
                "shape is bundle-derived and unverified; see "
                "capture/BUNDLE_DERIVED.md.")
            return {"success": False, "skipped": "unconfirmed-broadcast"}
        # The ordinary send rules apply unchanged — a broadcast is a message.
        check_sendable(media_files=media_files, price=price)
        group_id = self._broadcast_group(include_lists=include_lists,
                                         exclude_lists=exclude_lists,
                                         tier_id=tier_id)
        log.warning("fansly: BROADCASTING to group %s (include=%s exclude=%s)",
                    group_id, include_lists, exclude_lists)
        created = self.send_message(group_id, text, media_files=media_files,
                                    price=price, _broadcast=True)
        return {"success": True, "groupId": group_id,
                "id": str(created.get("id")) if created.get("id") else None}

    # OF's win-back list. Needs a subscription that has actually lapsed.
    @fansly_blocked("needs an expired subscription to capture",
                    returns=lambda: {"list": [], "hasMore": False})
    def recent_expired_subscribers(self, *_a: Any, **_k: Any) -> dict: ...

    # Money movement. auto_follow already falls back to follow_user when this
    # raises or returns falsy, so a clean skip keeps that path working.
    @fansly_blocked("resubscribe moves money — needs a payment method",
                    returns=lambda: {"success": False, "skipped": "not-wired"})
    def resubscribe_user(self, *_a: Any, **_k: Any) -> dict: ...

    def delete_promo(self, promo_id: str | int, **_ignored: Any) -> dict:
        """OF `DELETE /promotions/{id}` -> Fansly disables a gift code by
        re-POSTing the WHOLE object with `status: 2` (captured 2026-09-02 from
        the "Disable" button on /creator/plans). There is no delete verb; the
        code is fetched first so nothing but the status changes."""
        codes = self._request("GET", "/subscriptions/giftcodes") or []
        cur = next((g for g in codes if isinstance(g, dict)
                    and str(g.get("id")) == str(promo_id)), None)
        if not cur:
            return {"success": False, "skipped": "not-found"}
        self._request("POST", "/subscriptions/giftcodes",
                      json_body={**cur, "status": 2})
        return {"success": True}

    def _first_plan(self) -> dict | None:
        """The plan a promo hangs off: the first active plan of the first
        tier (GET /subscriptions/tiers). Fansly promos are per PLAN, OF's are
        per account, so a multi-tier account gets the promo on tier 0 only —
        that limitation is surfaced in the returned object, not hidden."""
        tiers = self._request("GET", "/subscriptions/tiers") or []
        for t in tiers if isinstance(tiers, list) else []:
            for p in (t.get("plans") or []):
                if p.get("status") == 1 and p.get("id"):
                    return {**p, "tierId": t.get("id"), "tierName": t.get("name")}
        return None

    def create_promo(self, *, subscribe_counts: int = 1, subscribe_days: int = 30,
                     discount: int | None = None, message: str = "",
                     **_ignored: Any) -> dict:
        """OF `POST /promotions` -> Fansly `POST /subscriptions/giftcodes`.

        Captured 2026-09-02 from the creator UI ("Create Discount" on
        /creator/plans, see RECIPES "Subscription tiers"). Fansly's promo is a
        GIFT CODE on one plan: a discounted first period (`price`, 1/1000 $)
        for `duration` days, `maxUses` claims, after which the plan renews at
        full price. OF's `discount` percent maps onto the plan price; OF's
        `subscribe_days` -> `duration`; `subscribe_counts` -> `maxUses`;
        `message` -> `label`. Returns an OF-ish promo object so
        `of_promos.normalise_of_promo` can read `id` / `price` / liveness."""
        if discount is None:
            raise ValueError("create_promo requires discount (integer percent 1-100)")
        pct = max(1, min(int(discount), 100))
        plan = self._first_plan()
        if not plan:
            raise FanslyAPIError("Cannot create a Fansly promo: the account has "
                                 "no active subscription plan (create a tier first).")
        full = int(plan.get("price") or 0)
        promo_price = int(round(full * (100 - pct) / 100.0))
        created = self._request("POST", "/subscriptions/giftcodes", json_body={
            "id": "", "label": message or "", "planId": str(plan["id"]),
            "price": promo_price, "duration": int(subscribe_days),
            "maxUses": int(subscribe_counts), "newSubscribersOnly": 1,
            "startsAt": 0, "endsAt": 0,
        }) or {}
        return {
            "id": str(created.get("id") or ""),
            "price": (created.get("price") or 0) / 1000.0,
            "subscribeCounts": created.get("maxUses"),
            "subscribeDays": created.get("duration"),
            "message": created.get("label") or "",
            "isFinished": created.get("status") != 1,
            "canClaim": created.get("status") == 1,
            "claimsCount": created.get("uses") or 0,
            "fanslyGiftCode": {"code": created.get("code"), "planId": created.get("planId"),
                               "tierId": plan.get("tierId"),
                               "link": f"https://fansly.com/subscriptions/giftcode/{created.get('code')}"
                               if created.get("code") else None},
        }

    # Deliberately not implemented rather than blocked-with-a-reason would be
    # wrong here: the endpoint mirrors like_message on the post resource, but
    # exercising it needs a post with media and the second-opinion review cut
    # it as peripheral (YAGNI — no go-live capability). auto_follow reads the
    # favourite count before and after to decide what happened, so a falsy
    # return leaves that comparison unchanged and the fan simply is not liked.
    @fansly_blocked("post likes are out of go-live scope (YAGNI)",
                    returns=lambda: {"success": False, "skipped": "not-wired"})
    def like_post(self, *_a: Any, **_k: Any) -> dict: ...

    def _group_for_send(self, chat_id: str | int) -> str:
        """The group a SEND goes to — resolved like `_as_group_id`, but a fan
        with no DM thread yet gets one MINTED (`POST /group`, the base client's
        `create_group`) instead of being passed through as-is.

        Why this exists: `_as_group_id` returns the raw id when nothing resolves,
        which is right for reads (a stranger's thread simply has no messages)
        but wrong for a send — Fansly answers `POST /message` with a fan id in
        `groupId` as a 500 `'error getting group'`. That is exactly the
        new-follower case send_welcome lives for (proven live 2026-09-02: the
        phase-0 follower had followed, never DM'd, and every welcome 500'd).
        A follow does NOT open a group on Fansly; the first message does.

        Reads must keep `_as_group_id`: minting a thread from a mark-read or a
        typing ping would create groups for fans nobody ever wrote to."""
        chat_id = str(chat_id)
        resolved = self._as_group_id(chat_id)
        if resolved != chat_id or chat_id in self._known_group_ids:
            return resolved
        # `find_group_with` already came back empty inside `_as_group_id`, and
        # it scans the 100 most-recent groups — re-scan the WHOLE listing before
        # minting, so an old thread outside that window does not get a duplicate.
        existing = self.find_group_with(chat_id, scan_limit=10_000)
        if existing:
            self._known_group_ids.add(existing)
            self._fan_to_group[chat_id] = existing
            return existing
        created = self.create_group(chat_id)
        if not created:
            raise FanslyAPIError(
                f"Fansly accepted POST /group for fan {chat_id} but returned "
                f"no group id, so there is no thread to send into."
            )
        self._known_group_ids.add(created)
        self._fan_to_group[chat_id] = created
        log.info("fansly: minted DM group %s for fan %s (first message)",
                 created, chat_id)
        return created

    def _as_group_id(self, chat_id: str | int) -> str:
        """Accept a groupId (pass through) or a fan accountId (resolve to its
        group), so the shim tolerates either OF or Fansly id conventions.

        Fast path: if it is a groupId we already emitted from list_chats, use it
        directly. Only a stranger id pays for the find_group_with scan (which
        matches on partnerAccountId, so a groupId would never match anyway)."""
        chat_id = str(chat_id)
        if chat_id in self._known_group_ids:
            return chat_id
        mapped = self._fan_to_group.get(chat_id)
        if mapped:
            return mapped
        resolved = self.find_group_with(chat_id)
        if resolved:
            self._known_group_ids.add(resolved)
            return resolved
        return chat_id
