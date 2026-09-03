"""
The creator vault: reads, album folders, and the folder WRITE side.

Split out of `fansly_shim.py` — this is one cohesive surface (albums + the
media in them) with a narrow seam into the rest of the client: it needs
`self._request` and `self.session`, nothing else. Keeping it here means the
vault can be read end to end in one screenful of context instead of being
scattered through a 3k-line file, and `fansly_shim.py` stops growing every
time the vault gains a method.

It is a MIXIN, not a helper object, because these are OF-client methods:
callers do `client.vault_lists(...)`, and the shim's own send path calls
`self.vault_media_by_id` / `self._of_media_for_ids`. A separate collaborator
would need a back-reference to the client and buy nothing.
"""
from __future__ import annotations

from typing import Any

# OF names its per-kind folder counts with these keys and the frontend's folder
# subtitle is built from exactly them; `photo`/`video`/`gif`/`audio` are what
# `of_media` resolves a raw Fansly media `type` into.
_COUNT_KEYS = {"photo": "photosCount", "video": "videosCount",
               "gif": "gifsCount", "audio": "audiosCount"}

# A folder with nothing in it, and the shape every counted folder starts from.
_EMPTY_COUNTS: dict[str, Any] = {
    **dict.fromkeys(_COUNT_KEYS.values(), 0), "mediaCount": 0, "hasMedia": False,
}


def _of_media(account_media: dict) -> dict:
    """`fansly_shim.of_media`, imported late.

    The reshapers live in `fansly_shim` and the shim imports THIS module to
    build its client, so a module-level import here would close the cycle.
    `of_media` is a pure function over a dict, so resolving it per call costs
    a cached module lookup and keeps the dependency pointing one way."""
    from fansly_shim import of_media
    return of_media(account_media)


class VaultMixin:
    """OF's vault surface, spoken to Fansly's album API.

    `_request` and `session` come from `FanslyClient`; this class never
    constructs them, which is what keeps the seam narrow enough to test the
    vault against a fake client with no network at all.
    """

    # -- Vault (real: GET /uservault/albumsnew) ------------------------------
    # Fansly's vault is "albums" on /uservault/albumsnew?accountId=<us>, which
    # returns {albums[], aggregationData{media, accountMedia,
    # accountMediaBundles, albumContent}}. An album is
    # {id, accountId, public, pos, title, description, type} — type 2007 is the
    # built-in "Purchases" album; user-made albums carry their own type. The
    # media themselves are NOT inline: albumContent maps albumId -> mediaId, and
    # the accountMedia[] bucket carries the objects, exactly like /message does.
    # Verified live read-only (our account: 1 album "Purchases", type 2007).

    def _vault_media_payload(self, *, album_id: str | None = None) -> list:
        """The creator's raw vault media. RESTORED after I accidentally sliced it
        out while rewriting vault_lists — see the album filter note below.

        `album_id` uses **albumId** deliberately: `albumIds` is accepted and
        SILENTLY IGNORED (an album with itemCount 0 returns the whole library
        under albumIds, 0 under albumId — verified live)."""
        params: dict[str, Any] = {"accountId": str(self.session.account_id)}
        if album_id:
            params["albumId"] = str(album_id)
        out = self._request("GET", "/media/vault", params=params)
        return out if isinstance(out, list) else []

    # The REAL creator vault is /vault/albums + /media/vault — NOT
    # /uservault/albumsnew, which only ever returns the type-2007 "Purchases"
    # album (BOUGHT content) and is why the vault looked empty for a creator
    # whose own media was sitting right there. Live: /vault/albums returns the
    # user's actual albums with itemCount, /media/vault returns the raw media.
    #
    # FILTER PARAM WARNING: `/media/vault` honours **albumId**. `albumIds` is
    # SILENTLY IGNORED — verified live: an album with itemCount 0 returned 1
    # item under albumIds and 0 under albumId. Wrong param looks like it works.

    # Recovered from the Fansly web bundle (main.14d24b3e179efdb7.js), where the
    # client re-titles API albums by matching `album.type` — a LITERAL enum, not
    # an inference. Keyed off its ServiceIds table (PostService:1,
    # MessageService:5), so the album types are 1000*serviceId:
    #     38000 -> "All"        (literal `type:38e3,title:"All"`)
    #      5000 -> "Messages"   (1e3 * MessageService)
    #      1000 -> "Posts"      (1e3 * PostService)
    #      2007 -> "Purchases"  (handled separately by the client)
    # Anything else is a user album and keeps whatever title the API gave it.
    #
    # DO NOT add `1: "Posts"`. Posts is **1000**, not 1. This account really does
    # have a type-1 album (itemCount 0, title null) which falls through to the
    # custom branch — the "Posts" tile in the web UI is a SYNTHETIC album the
    # client unshifts client-side (id null, type 1000), not the API's type-1 row.
    # Labelling type 1 "Posts" from its zero count would be a confident wrong
    # name, which is worse than an ugly one.
    _VAULT_ALBUM_NAMES = {
        "38000": "All",
        "5000": "Messages",
        "1000": "Posts",
        "2007": "Purchases",
    }

    def _albums(self) -> list[dict]:
        """Every album this account owns, position-ordered — the ONE read.

        `/vault/albums` is scoped by the auth token, so the `accountId` param
        the web client sends changes nothing (verified live: identical bodies
        with and without it). It is sent anyway to match every other vault read
        in this class; what matters is that there is a single copy of that
        decision. Three call sites used to spell this GET out by hand and two
        had already drifted apart on that param — the same hand-copy drift
        `check_sendable` was collapsed to fix."""
        albums = self._request(
            "GET", "/vault/albums",
            params={"accountId": str(self.session.account_id)},
        )
        rows = [a for a in (albums or []) if isinstance(a, dict) and a.get("id")]
        rows.sort(key=lambda a: a.get("pos") or 0)
        return rows

    def vault_lists(self, *, view: str = "main", limit: int = 10,
                    offset: int = 0, **_ignored: Any) -> dict:
        """OF `/vault/lists` shape — vault folders. Fansly: GET /vault/albums.

        Fansly returns `title: null` on these and its own web client derives the
        display names (the user sees Posts / All / Messages) from `type`
        client-side. We do NOT guess that mapping: an album with no title gets a
        stable `Album <type>` label until a vault capture gives us the real
        table. A wrong-but-confident folder name is worse than an ugly one.

        PER-KIND COUNTS are read from each album's own media, NOT from the
        album's `itemCount`. Two reasons, both verified live on this account:
        `itemCount` is stale/wrong (the type-38000 album reports 1 and returns
        2 items), and it is a single total, while the folder subtitle needs the
        photo/video/gif/audio split. OF supplies that split on the folder row,
        and the frontend's subtitle is built ONLY from those four fields — with
        them missing every folder rendered "empty" no matter what `hasMedia`
        said. The mirror can't fill the gap either: it derives membership from
        OF's per-item `listStates[]`, which Fansly media simply do not carry.

        Cost is one `albumId`-filtered read per album ON THE PAGE (~55ms each,
        measured), which is why the slice happens BEFORE the counting rather
        than after — a limit=10 page never pays for albums nobody asked for.
        `_album_contents` caches those reads, so paging through the folder list
        sweeps each album at most once."""
        albums = self._albums()
        page_albums = albums[offset:offset + limit] if limit else albums
        page = [{**self._album_row(a), **self._album_counts(a)} for a in page_albums]
        return {"list": page, "hasMore": offset + len(page) < len(albums)}

    def _album_row(self, a: dict) -> dict:
        """One album -> the OF folder-row identity fields.

        The single home for how a Fansly album is named and classified. Both
        the folder listing and the per-item `listStates[]` render this, and
        they MUST agree: the mirror keeps only `type == "custom"` entries, so
        a built-in classified as custom would make every item look like it
        lived in "All" and "Messages" too. This used to be spelled out at both
        call sites — the same hand-copy drift `_albums` was collapsed to fix."""
        atype = str(a.get("type") or "")
        return {
            # STRING id — snowflake >2^53, and it round-trips back as
            # `list_id`. See the VaultList.id blocker in the status doc.
            "id": str(a.get("id") or ""),
            # OF's picker branches on "custom"; the built-ins are not
            # user-editable so they're reported distinctly.
            "type": ("purchases" if atype == "2007"
                     else "builtin" if atype in self._VAULT_ALBUM_NAMES
                     else "custom"),
            "name": (a.get("title")
                     or self._VAULT_ALBUM_NAMES.get(atype)
                     or f"Album {atype}"),
        }

    def _album_contents(self, albums: list[dict]) -> dict[str, list[dict]]:
        """album id -> its media, for the albums asked for. Swept once each.

        Fansly exposes membership nowhere else: raw vault media carry no album
        field and album rows carry no member ids, so the only source is an
        `albumId`-filtered read per album (~55ms). Both things we need — how
        many of each kind a folder holds, and which folders an item is in —
        are projections of this one read, so it is fetched once and read twice
        rather than walked separately per caller.

        CACHED PER ALBUM, and only the missing ones are fetched. Two callers
        want different slices: the folder listing wants just the page it is
        about to return (a limit=10 page must not pay for albums nobody asked
        for), while `vault_media` needs every album to say which ones an item
        belongs to. A per-album cache serves both without either over-fetching,
        and it matters because `vault_media` is PAGINATED over a library-wide
        answer: recomputing per page made a full collect (100 items a page over
        thousands) re-sweep every album for every page — 20 albums over 60
        pages is 1200 reads for something that never changed. Every album write
        drops the cache, so an edit is visible immediately. Same lazy-rebuild
        shape as the client's own `_digest_cache`.

        A read that fails yields an empty album rather than propagating — a
        folder listing must survive one unreadable album."""
        cache = getattr(self, "_albums_media", None)
        if cache is None:
            cache = self._albums_media = {}
        for a in albums:
            aid = str(a.get("id") or "")
            if not aid or aid in cache:
                continue
            try:
                media = self._vault_media_payload(album_id=aid)
            except Exception:
                media = []
            cache[aid] = [m for m in media if isinstance(m, dict) and m.get("id")]
        return cache

    def _album_counts(self, album: dict) -> dict:
        """One album's folder-row count fields.

        Kinds come from `of_media`, not the raw `type` int, so a subtitle can
        never disagree with the grid the same resolver fills."""
        tally = dict(_EMPTY_COUNTS)
        for m in self._album_contents([album]).get(str(album.get("id") or ""), []):
            kind = _of_media({"id": m["id"], "media": m, "access": True}).get("type")
            key = _COUNT_KEYS.get(kind)
            if key:
                tally[key] += 1
            tally["mediaCount"] += 1
        tally["hasMedia"] = tally["mediaCount"] > 0
        return tally

    def _album_membership(self) -> dict[str, list[dict]]:
        """media id -> the `listStates[]` entries naming the albums holding it.

        Needs EVERY album, unlike the folder listing: an item's folder list is
        only correct if every album was checked for it."""
        albums = self._albums()
        contents = self._album_contents(albums)
        out: dict[str, list[dict]] = {}
        for a in albums:
            state = {**self._album_row(a), "hasMedia": True}
            for m in contents.get(state["id"], []):
                out.setdefault(str(m["id"]), []).append(state)
        return out

    def _vault_write(self, path: str, body: dict) -> Any:
        """Every album mutation goes through here, so none can forget to drop
        the memoized `_album_contents` cache.

        The alternative was an invalidate call in each of the six write
        methods, which is six chances to omit one and serve a stale folder
        count after an edit. Routing the writes through one seam makes the
        invalidation structural instead of a convention a future method has to
        remember. Every POST this mixin makes is an album mutation, so there is
        nothing else that would want the un-invalidating path."""
        out = self._request("POST", path, json_body=body)
        self._albums_media = None
        return out

    def vault_media(self, *, limit: int = 24, offset: int = 0,
                    type: str = "all", list_id: int | None = None,
                    **_ignored: Any) -> dict:
        """OF `/vault/media` shape — the creator's media library.

        Fansly serves it as a flat list of RAW media at `/media/vault`. Each is
        wrapped as an accountMedia envelope so the existing `of_media` resolves
        it: it's our own media, so access=True → canView True, and full/preview/
        thumb come from the raw object's `locations[]`/`variants[]`.

        Album (`list_id`) filtering IS wired, server-side, via **albumId** —
        `albumIds` is silently ignored and would return the whole library while
        looking like it filtered. `sort`/`query` are accepted and ignored (no
        server-side ordering/search on this list).

        Each item carries `listStates[]`, the OF field naming the folders that
        media belongs to. Fansly's raw media has NO album back-reference, so it
        is assembled here, where the albums are known. It is not decoration:
        the mirror derives `of_folder_ids` from exactly this field, and the
        panel filters a folder view through `of_folder_ids` once an account is
        collected. Without it the mirror recorded every Fansly media as
        belonging to nothing, so picking a folder filtered nothing away and the
        grid showed the whole library under that folder's heading."""
        member_of = self._album_membership()
        items = []
        for m in self._vault_media_payload(album_id=str(list_id) if list_id is not None else None):
            if not isinstance(m, dict) or not m.get("id"):
                continue
            om = _of_media({"id": m["id"], "media": m, "access": True})
            if type and type != "all" and om.get("type") != type:
                continue
            om["isReady"] = True
            om["hasError"] = False
            om["listStates"] = member_of.get(str(m["id"]), [])
            items.append(om)
        page = items[offset:offset + limit] if limit else items
        return {"list": page, "hasMore": offset + len(page) < len(items)}

    def vault_media_by_id(self, media_id: str | int, **_ignored: Any) -> dict:
        """OF `/vault/media/{id}` — resolve ONE bare media id back to a media
        object. The Brain panel stores only ids per slot, so without this the
        route AttributeError'd -> 500 and those thumbnails stayed blank.

        `GET /media?ids=` returns the RAW media, so it is wrapped in the same
        accountMedia envelope `vault_media` uses (`access: True` — it is our own
        library) to reach the shared `of_media` resolver. Returns {} when the id
        doesn't resolve, rather than inventing a placeholder."""
        rows = self._request("GET", "/media", params={"ids": str(media_id)})
        row = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(row, dict) or not row.get("id"):
            return {}
        om = _of_media({"id": row["id"], "media": row, "access": True})
        om["isReady"] = True
        om["hasError"] = False
        return om

    def _of_media_for_ids(self, media_ids: list) -> list:
        """OF media objects for raw vault ids, in the order given.

        ONE batched `/media?ids=a,b,c` — the send path calls this once, after
        the message is already delivered, so it costs a single round-trip and
        never blocks the send itself. Best-effort: any failure yields [] and
        the caller simply omits media rather than failing a send that worked."""
        ids = [str(m) for m in media_ids if m]
        if not ids:
            return []
        try:
            rows = self._request("GET", "/media", params={"ids": ",".join(ids)}) or []
        except Exception:
            return []
        by_id = {str(r.get("id")): r for r in rows if isinstance(r, dict) and r.get("id")}
        out = []
        for mid in ids:
            raw = by_id.get(mid)
            if raw:
                out.append(_of_media({"id": raw["id"], "media": raw, "access": True}))
        return out

    # -- Vault folders: the WRITE side (real, live-verified 2026-09-02) -------
    # OF calls them "lists" under /vault/lists; Fansly calls them ALBUMS and
    # every mutation is a POST to a distinct path (no PATCH/DELETE verbs at
    # all). Recovered from the web bundle (main.14d24b3e179efdb7.js, the same
    # one the album-type table came from) and then each one exercised live
    # against a throwaway album on this account:
    #
    #   POST /vault/albums              {id,title,description,accountId,type}
    #   POST /vault/albums/edit         the WHOLE album object back, changed
    #   POST /vault/albums/delete       {albumId}
    #   POST /vault/albums/media        {albumId, mediaIds[]}
    #   POST /vault/albums/media/delete {albumId, mediaIds[]}
    #   POST /vault/albums/order        {oldPos, newPos}
    #
    # Create takes `id:null, accountId:null, type:0` — the server assigns all
    # three, and the album comes back with `type: null` (NOT 0), which is why
    # `vault_lists` classifies an unknown type as "custom" rather than keying
    # off a literal 0.
    #
    # IDS STAY STRINGS. Album and media ids are snowflakes above 2^53; the OF
    # signatures say `int` because OF's really are ints, but coercing a Fansly
    # one to a float anywhere in the round trip silently lands on a DIFFERENT
    # album. Everything here takes what it is given and str()s it.

    def create_vault_list(self, name: str) -> dict:
        """OF `POST /vault/lists`. Fansly: POST /vault/albums.

        Returns the SAME row shape `vault_lists` emits, so a freshly created
        folder and a listed one are one type to every caller. The live response
        carries no counts, hence the zeros and `hasMedia: False` — true by
        construction for a folder created empty, not a guess, so the per-kind
        counts are filled in here rather than paying for a read that can only
        come back empty."""
        album = self._vault_write("/vault/albums", {
            "id": None,
            "title": name,
            "description": "",
            "accountId": None,
            "type": 0,
        }) or {}
        return {
            "id": str(album.get("id") or ""),
            "type": "custom",
            "name": album.get("title") or name,
            **_EMPTY_COUNTS,
        }

    def _album_media(self, path: str, list_id: Any, media_ids: list,
                     counted: str) -> dict:
        """Add or remove album membership — the two are the same call to two
        paths, so they are one implementation. Fansly answers both with the
        link rows it touched; that count is what the caller reports.

        An empty id list short-circuits: Fansly would accept it and do nothing,
        but a round trip to confirm we asked for nothing is pure latency."""
        ids = [str(m) for m in (media_ids or []) if m]
        if not ids:
            return {"success": True, counted: 0}
        rows = self._vault_write(path, {"albumId": str(list_id), "mediaIds": ids})
        return {"success": True,
                counted: len(rows) if isinstance(rows, list) else len(ids)}

    def add_media_to_vault_list(self, list_id: Any, media_ids: list) -> dict:
        """OF `POST /vault/lists/{id}/media`. Fansly: POST /vault/albums/media.
        Additive, exactly like OF's — re-adding media already in the album is
        not an error."""
        return self._album_media("/vault/albums/media", list_id, media_ids, "added")

    def remove_media_from_vault_list(self, list_id: Any, media_ids: list) -> dict:
        """POST /vault/albums/media/delete — no OF counterpart (OF's folders are
        add-only through this client), but Fansly gives it to us for free and
        the vault panel's 'remove from folder' needs it."""
        return self._album_media("/vault/albums/media/delete", list_id, media_ids, "removed")

    def rename_vault_list(self, list_id: Any, name: str) -> dict:
        """OF `PATCH /vault/lists/{id}`. Fansly: POST /vault/albums/edit, which
        takes the WHOLE album back, not a patch — its web client posts the album
        object it already holds. We re-read the album first so the description
        and type survive a rename; a blind post would blank them."""
        current = next((a for a in self._albums()
                        if str(a["id"]) == str(list_id)), {})
        updated = self._vault_write("/vault/albums/edit", {
            "id": str(list_id),
            "title": name,
            "description": current.get("description") or "",
            "accountId": str(self.session.account_id),
            "type": current.get("type") if current.get("type") is not None else 0,
        }) or {}
        return {
            "id": str(updated.get("id") or list_id),
            "name": updated.get("title") or name,
            "type": "custom",
        }

    def delete_vault_list(self, list_id: Any, clear_media: bool = False) -> dict:
        """OF `DELETE /vault/lists/{id}`. Fansly: POST /vault/albums/delete
        {albumId}. Fansly has no `clearMedia` equivalent — deleting an album
        never deletes the media in it — so the flag is accepted and ignored
        rather than silently doing something different from what it says."""
        self._vault_write("/vault/albums/delete", {"albumId": str(list_id)})
        return {"success": True}

    def sort_vault_lists(self, sort: str = "recent", order: str = "desc") -> dict:
        """OF `POST /vault/lists/sort` (a named ordering). Fansly has no such
        endpoint: its only ordering primitive is /vault/albums/order, which
        moves ONE album from oldPos to newPos. A name-based sort is therefore
        not expressible, and reporting success would leave the panel showing an
        order the server never applied."""
        return {"success": False, "unsupported": "fansly_sort_by_name"}

    def set_vault_lists_custom_order(self, list_ids: list) -> dict:
        """OF `POST /vault/lists/sort` with a manual order. Fansly: repeated
        POST /vault/albums/order {oldPos,newPos} — one move per album.

        Replays the target order as a sequence of moves against a live copy of
        the current positions, which is what dragging in Fansly's own UI emits.
        Albums not named in `list_ids` keep their relative order after them."""
        cur = [str(a["id"]) for a in self._albums()]
        want = [str(i) for i in (list_ids or []) if str(i) in cur]
        if not want:
            return {"success": True, "moves": 0}
        moves = 0
        for target_index, album_id in enumerate(want):
            old = cur.index(album_id)
            if old == target_index:
                continue
            self._vault_write("/vault/albums/order",
                              {"oldPos": old, "newPos": target_index})
            cur.insert(target_index, cur.pop(old))
            moves += 1
        return {"success": True, "moves": moves}

    def vault_media_types(self, **_ignored: Any) -> dict:
        """OF `/vault/media/types` — the kinds the vault can hold. Fansly has no
        such endpoint; these are the types `of_media` actually emits, so the
        filter UI offers exactly what the list can return."""
        return {"types": ["photo", "video", "gif", "audio"]}

    def vault_media_processing(self, **_ignored: Any) -> dict:
        """OF `/vault/media/processing` — uploads still transcoding. Honestly
        empty: we never upload on the Fansly path, so nothing of ours is ever
        in flight. (Not a stub hiding work — there is no work to show.)"""
        return {"list": [], "hasMore": False}
