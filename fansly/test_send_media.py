"""
Prove the vault-media send reproduces the captured browser flow — offline.

Sending vault media is TWO calls (see FanslyShimClient._attach_media), and the
first one's body is fiddly enough (a doubly-encoded price blob, a 1/1000-dollar
unit, a whitelist naming both parties) that a plausible-looking regression would
still deliver a dead attachment or silently undercharge a PPV by 10x. So this
asserts our outgoing bodies against "vault and send.har" FIELD BY FIELD rather
than merely checking the send didn't raise.

No network: _request is swapped for a recorder.

    ../venv/bin/python test_send_media.py [path/to/"vault and send.har"]
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

from fansly_client import FanslyAPIError, FanslySession
from fansly_shim import FanslyShimClient, check_sendable, of_messages_page

HERE = Path(__file__).resolve().parent
DEFAULT_HAR = HERE / "vault and send.har"
US = "789937824869654528"      # our account, in the capture
FAN = "932808301660352512"     # the recipient, in the capture
GROUP = "950834531911344131"
MEDIA = "950890681742675973"


def _captured(har_path: Path, path: str) -> list[dict]:
    """Every POST body the browser sent to `path`, in capture order."""
    har = json.loads(har_path.read_text())
    out = []
    for entry in har["log"]["entries"]:
        req = entry["request"]
        if req["method"] != "POST" or urlsplit(req["url"]).path != path:
            continue
        text = (req.get("postData") or {}).get("text")
        if text:
            out.append(json.loads(text))
    return out


def _session_doing(put_fn):
    """The per-part session seam, parametrised by the only thing that varies.

    Every offline upload test needs the same stand-in for the fresh
    `requests.Session` each part worker opens — `proxies`, `close()`, and a
    `put` that does something specific. Three near-identical classes left the
    reader diffing boilerplate to find the one line that mattered.
    """
    class _Session:
        proxies: dict = {}

        def put(self, url, data=None, headers=None, timeout=None):
            return put_fn(url, data=data, headers=headers, timeout=timeout)

        def close(self):
            pass

    return _Session


def _client() -> tuple[FanslyShimClient, list]:
    """A shim wired to the captured account whose _request only records."""
    c = FanslyShimClient(FanslySession({
        "accountId": US, "token": "t", "check_key": "k", "device_id": "d",
        # A real UA is required by FanslySession — nothing here reaches the
        # network, but the constructor refuses a session that couldn't sign.
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    }))
    calls: list[tuple[str, str, object]] = []

    def fake(method, path, *, params=None, json_body=None, **_kw):
        calls.append((method, path, json_body))
        if path == "/media" and method == "GET":
            # The post-send lookup that repopulates the response's media[].
            return [{"id": MEDIA, "type": 1, "status": 1, "mimetype": "image/jpeg",
                     "locations": [], "variants": []}]
        if path == "/account/media":
            # Echo a DISTINCT envelope id per call — the whole point is that the
            # message must attach this, not the mediaId it was handed.
            return [{"id": f"ACCTMEDIA{len(calls)}"}]
        if path == "/message":
            return {"id": "1", "groupId": GROUP, "senderId": US,
                    "content": "", "createdAt": 1.0, "attachments": []}
        raise AssertionError(f"unexpected call {method} {path}")

    c._request = fake
    c._known_group_ids.add(GROUP)
    c._group_to_fan[GROUP] = FAN
    return c, calls


def main(argv: list[str]) -> int:
    har = Path(argv[1]) if len(argv) > 1 else DEFAULT_HAR
    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    # -- the captured browser bodies, as ground truth ----------------------
    if har.exists():
        grants = _captured(har, "/api/v1/account/media")
        msgs = _captured(har, "/api/v1/message")
        check(grants and msgs, "no /account/media or /message POSTs in the HAR")
        free = next((g[0] for g in grants
                     if not g[0]["permissions"]["permissionFlags"]), None)
        paid = next((g[0] for g in grants
                     if g[0]["permissions"]["permissionFlags"]), None)
        check(free is not None, "HAR has no FREE grant to compare against")
        check(paid is not None, "HAR has no PAID grant to compare against")
    else:
        print(f"note: {har.name} absent — comparing against inlined constants",
              file=sys.stderr)
        free = paid = None

    # -- 1. free send: one grant, then one message attaching the ENVELOPE ---
    c, calls = _client()
    c.send_message(GROUP, text="", media_files=[MEDIA])
    order = [p for _, p, _ in calls if p != "/media"]
    check(order == ["/account/media", "/message"],
          f"free send should be grant-then-message, got {order}")
    grant = calls[0][2][0]
    check(grant["mediaId"] == MEDIA, "grant must carry the vault mediaId")
    check(grant["permissionFlags"] == 8, "envelope permissionFlags must be 8")
    check(grant["price"] == 0, "top-level grant price is always 0")
    check(grant["permissions"]["permissionFlags"] == [],
          "a free grant carries NO permission flags")
    check(sorted(w["accountId"] for w in grant["whitelist"]) == sorted([US, FAN]),
          "whitelist must name both us and the recipient")
    att = next(b for _, p_, b in calls if p_ == "/message")["attachments"]
    check(len(att) == 1 and att[0]["contentId"] == "ACCTMEDIA1",
          "message must attach the ACCOUNTMEDIA id, never the raw mediaId")
    check(att[0]["contentId"] != MEDIA,
          "attaching the raw mediaId is accepted by Fansly and delivers a dead "
          "attachment — this is the regression this test exists for")
    check(att[0]["contentType"] == 1 and att[0]["pos"] == 0,
          "attachment must be contentType 1 at pos 0")
    if free is not None:
        check(grant.keys() == free.keys(),
              f"free grant keys drifted from the capture: "
              f"{set(grant) ^ set(free)}")

    # -- 2. paid send: the price rides on the grant, in 1/1000 dollars ------
    c, calls = _client()
    c.send_message(GROUP, text="", media_files=[MEDIA], price=101)
    pf = calls[0][2][0]["permissions"]["permissionFlags"]
    check(len(pf) == 1, "a paid grant carries exactly one permission flag")
    check(pf[0]["price"] == 101000,
          f"$101 must wire as 101000 (1 USD = 1000), got {pf[0]['price']}")
    check(pf[0]["type"] == 0 and pf[0]["flags"] == 3,
          "captured paid grant is type 0 / flags 3")
    check(pf[0]["metadata"] == '{"1":"{\\"price\\":101000}"}',
          f"metadata is DOUBLY json-encoded; got {pf[0]['metadata']!r}")
    check(calls[0][2][0]["price"] == 0,
          "the top-level price stays 0 even on a PPV — the price is in the flag")
    if paid is not None:
        check(pf[0] == {k: paid["permissions"]["permissionFlags"][0][k]
                        for k in pf[0]},
              "paid permission flag drifted from the captured browser body")

    # fractional dollars must not lose a cent
    c, calls = _client()
    c.send_message(GROUP, text="", media_files=[MEDIA], price=24.99)
    check(calls[0][2][0]["permissions"]["permissionFlags"][0]["price"] == 24990,
          "$24.99 must wire as 24990")

    # -- 3. multiple attachments keep one grant each, ordered by pos -------
    c, calls = _client()
    c.send_message(GROUP, text="", media_files=[MEDIA, MEDIA])
    check([p for _, p, _ in calls if p != "/media"]
          == ["/account/media", "/account/media", "/message"],
          "each attachment needs its OWN grant")
    att = next(b for _, p_, b in calls if p_ == "/message")["attachments"]
    check([a["pos"] for a in att] == [0, 1], "attachments must be pos-ordered")
    check([a["contentId"] for a in att] == ["ACCTMEDIA1", "ACCTMEDIA2"],
          "each attachment must carry its own envelope id, in order")

    # -- 3b. OF's `previews` = a FREE grant beside PAID ones ---------------
    # The teaser is expressible ONLY because price lives per-grant. Getting
    # this wrong charges for the teaser or gives away the paid set.
    c, calls = _client()
    c.send_message(GROUP, text="", media_files=["AAA", "BBB", "CCC"],
                   price=20, previews=["AAA"])
    grants = [b[0] for _, p_, b in calls if p_ == "/account/media"]
    check(len(grants) == 3, "one grant per attachment on a teaser send")
    by_media = {g["mediaId"]: g["permissions"]["permissionFlags"] for g in grants}
    check(by_media["AAA"] == [], "the previewed media must get a FREE grant")
    check(by_media["BBB"] and by_media["CCC"],
          "non-preview attachments must stay PRICED")
    check(by_media["BBB"][0]["price"] == 20000, "$20 must wire as 20000")
    check(all(g["previewId"] is None for g in grants),
          "previewId stays null — the browser never set it; previews are "
          "expressed as free sibling grants, not as a blurred stand-in")

    # on a FREE send previews are meaningless and must not alter the grants
    c, calls = _client()
    c.send_message(GROUP, text="", media_files=["AAA", "BBB"], previews=["AAA"])
    grants = [b[0] for _, p_, b in calls if p_ == "/account/media"]
    check(all(g["permissions"]["permissionFlags"] == [] for g in grants),
          "every grant on a free send is free, previews or not")

    # -- 4. the loud refusals — each would otherwise be a silent wrong ------
    for kwargs, why in [
        ({"price": 5},
         "a paid send with no media (the tease would go out FREE)"),
        ({"media_files": [{"processId": "x", "host": "h"}]},
         "an OF fresh-upload claim (only upload_media's vault id is sendable)"),
        ({"media_files": [MEDIA], "price": 10, "locked_text": True},
         "locked_text on a paid send (Fansly always shows text)"),
        ({"media_files": ["AAA", "BBB"], "price": 10,
          "previews": ["AAA", "BBB"]},
         "a paid send whose every attachment is a free preview (the fan would "
         "pay nothing and get everything while the UI reports a sale)"),
    ]:
        c, _ = _client()
        try:
            c.send_message(GROUP, text="hi", **kwargs)
            fails.append(f"should have refused {why}")
        except FanslyAPIError:
            pass

    # -- 4b. the shared checker: refuses the 4, allows everything else -----
    # check_sendable is the SINGLE source of truth for these rules — the shim
    # and the relay's schedule route both call it. They were briefly separate
    # hand-written copies and drifted within a day, so the allow-list half of
    # this matrix matters as much as the refuse half: an over-eager rule here
    # silently blocks legitimate sends in BOTH lanes at once.
    for kwargs, should_pass, label in [
        ({"media_files": [], "price": 0}, True, "plain text"),
        ({"media_files": [1, 2], "price": 0}, True, "free media"),
        ({"media_files": [1, 2], "price": 0, "previews": [1]}, True,
         "previews on a FREE send are a harmless no-op"),
        ({"media_files": [1, 2], "price": 20}, True, "paid media, none free"),
        ({"media_files": [1, 2], "price": 20, "previews": [1]}, True,
         "paid media with a 1-of-2 teaser"),
        ({"media_files": [1, 2], "price": 20, "previews": [9]}, True,
         "a preview id not among the attachments is ignored, not fatal"),
        ({"media_files": [1], "price": 0, "locked_text": True}, True,
         "locked_text on a FREE send is meaningless, not an error"),
        ({"media_files": [], "price": 20}, False, "paid with no media"),
        ({"media_files": [1, 2], "price": 20, "previews": [1, 2]}, False,
         "paid with EVERY attachment previewed"),
        ({"media_files": [{"processId": "x", "host": "h"}], "price": 0}, False,
         "an OF fresh-upload claim (processId/host, no vault id)"),
        ({"media_files": [{"vault_id": MEDIA, "send_with": [MEDIA]}],
          "price": 0}, True,
         "a fresh Fansly upload result (dict naming a vault id) — the "
         "refusal that used to block every fresh upload is gone"),
        ({"media_files": [{"mediaId": MEDIA}], "price": 20}, True,
         "a paid send of a dict-shaped vault id"),
        ({"media_files": [{"mediaId": MEDIA}], "price": 20,
          "previews": [MEDIA]}, False,
         "paid + every attachment previewed, even when dict-shaped"),
        ({"media_files": [1], "price": 20, "locked_text": True}, False,
         "locked_text on a PAID send"),
    ]:
        try:
            check_sendable(**kwargs)
            passed = True
        except FanslyAPIError:
            passed = False
        check(passed is should_pass,
              f"check_sendable {'allows' if should_pass else 'refuses'}: {label}")

    # -- 5. a free send must still be able to carry text + no media --------
    c, calls = _client()
    c.send_message(GROUP, text="just words")
    check([p for _, p, _ in calls] == ["/message"],
          "a text-only send must not mint a grant, nor look media up")
    check(calls[0][2]["attachments"] == [],
          "a text-only send must carry no attachments")

    # -- 5b. the SEND RESPONSE must describe what was actually sent --------
    # The frontend reconcile is `{...server}` — the response WINS over the
    # optimistic bubble (useChatMessages.mergeMedia). Fansly's own echo omits
    # both facts below, so returning it raw made a just-sent $101 PPV render
    # as free, and made a sent image VANISH (mergeMedia discards the optimistic
    # tiles when server.media is empty) until the next poll put it back.
    c, calls = _client()
    sent = c.send_message(GROUP, text="", media_files=[MEDIA], price=101)
    check(sent["price"] == 101.0,
          f"send response reports the price charged (got {sent['price']})")
    check(sent["isFree"] is False, "a paid send is not reported isFree")
    check(len(sent.get("media") or []) == 1,
          "send response carries the media, so the sent tile does not vanish")
    check(sent.get("mediaCount") == 1, "mediaCount matches")

    c, calls = _client()
    sent = c.send_message(GROUP, text="just words")
    check(not sent.get("media"), "a text-only send response carries no media")
    check(sent["price"] == 0 and sent["isFree"] is True,
          "a free send is reported free")

    # -- 6. a PPV must READ BACK as a PPV ----------------------------------
    # Fansly reports the MESSAGE at price 0 always; the real figure lives on
    # the attachment's grant. Live check found two genuine $101 sends reading
    # back as free, which makes OF's isPPV (price>0) false and strips the lock
    # chip, the PAID badge and the price from a message the fan WAS charged
    # for. Reconstructed here in the wire shape /message returns.
    def _grant(gid, amount):
        flags = ([] if not amount else
                 [{"type": 0, "flags": 11, "price": amount,
                   "metadata": '{"1":"{\\"price\\":%d}"}' % amount}])
        return {"id": gid, "accountId": US, "mediaId": MEDIA,
                "permissions": {"permissionFlags": flags},
                "media": {"id": MEDIA, "type": 1, "status": 1,
                          "mimetype": "image/jpeg", "locations": [],
                          "variants": []}}

    payload = {
        "messages": [
            {"id": "m_paid", "senderId": US, "content": "", "createdAt": 1.0,
             "attachments": [{"contentId": "g_paid", "contentType": 1, "pos": 0}]},
            {"id": "m_free", "senderId": US, "content": "", "createdAt": 1.0,
             "attachments": [{"contentId": "g_free", "contentType": 1, "pos": 0}]},
            {"id": "m_teaser", "senderId": US, "content": "", "createdAt": 1.0,
             "attachments": [{"contentId": "g_free", "contentType": 1, "pos": 0},
                             {"contentId": "g_paid", "contentType": 1, "pos": 1}]},
            {"id": "m_text", "senderId": US, "content": "hi", "createdAt": 1.0,
             "attachments": []},
            # TWO grants at the SAME price — exactly what _attach_media
            # produces for a multi-media PPV (one price applied to every
            # non-preview grant). This is the case that separates max from
            # sum; the teaser case below cannot, since its free grant adds 0.
            {"id": "m_two_paid", "senderId": US, "content": "", "createdAt": 1.0,
             "attachments": [{"contentId": "g_paid", "contentType": 1, "pos": 0},
                             {"contentId": "g_paid2", "contentType": 1, "pos": 1}]},
        ],
        "accountMedia": [_grant("g_paid", 101000), _grant("g_free", 0),
                         _grant("g_paid2", 101000)],
        "accountMediaBundles": [],
    }
    page = {m["id"]: m for m in of_messages_page(payload, account_id=US, limit=10)["list"]}
    check(page["m_paid"]["price"] == 101.0,
          f"a 101000-unit grant reads back as $101.00 (got {page['m_paid']['price']})")
    check(page["m_paid"]["isFree"] is False, "a priced message is not isFree")
    # The lock chip. MessageList renders `unlocked = isPPV && (isOpened ||
    # isPaid)`, and isOpened was hardcoded True — so a $5 PPV the fan had NOT
    # bought showed a green "✓ $5.00 unlocked" chip. Telling a chatter a fan
    # paid when they did not is the worst available direction to be wrong in,
    # so a priced message must report NOT opened until a purchase is proven.
    check(page["m_paid"]["isOpened"] is False,
          "a PPV is NOT 'opened' just because we sent it (the false-unlocked bug)")
    check(page["m_free"]["isOpened"] is True,
          "a free message stays opened — isPPV is false so no chip renders")
    for mid in ("m_paid", "m_two_paid", "m_teaser"):
        row = page[mid]
        unlocked = (row["price"] or 0) > 0 and (row.get("isOpened") or row.get("isPaid"))
        check(not unlocked, f"{mid} renders the 🔒 chip, never '✓ unlocked'")
    check(page["m_free"]["price"] == 0 and page["m_free"]["isFree"] is True,
          "a free grant stays free")
    check(page["m_text"]["price"] == 0, "a text-only message stays free")
    # The teaser case: OF's price is what the fan pays to UNLOCK, and Fansly
    # makes the teaser a free grant beside the paid one. Summing would double
    # -bill a $101 set to $101+0; max is the honest answer.
    check(page["m_two_paid"]["price"] == 101.0,
          f"a $101 set of TWO priced grants is a $101 message, not $202 — the "
          f"fan unlocks the message once (got {page['m_two_paid']['price']})")
    # OF's `previews` — which tiles ride FREE on a paid message. Fansly has no
    # such field, so it is derived: on a priced message a grant with NO price
    # IS the teaser. Without it the chat painted every tile of a teaser send
    # red "PPV-locked", including the one the fan can see for free.
    check(page["m_teaser"]["previews"] == ["g_free"],
          f"the free grant beside a paid one is reported as the teaser "
          f"(got {page['m_teaser']['previews']})")
    check(page["m_two_paid"]["previews"] == [],
          "a fully-locked set has no teaser")
    check(page["m_free"]["previews"] == [],
          "a FREE message has no teaser — nothing is locked to contrast with")
    check(page["m_text"]["previews"] == [], "a text-only message has no teaser")
    # Index-safe on purpose: when the derivation regresses to [] this must
    # report a FAIL like its siblings, not crash the whole run with an
    # IndexError and hide every check after it.
    teaser_ids = page["m_teaser"]["previews"]
    check(bool(teaser_ids) and teaser_ids[0] != MEDIA,
          "previews carry GRANT ids (what of_media puts in tile.id), not vault mediaIds")
    check(page["m_teaser"]["price"] == 101.0,
          f"a free teaser beside a $101 grant is a $101 message, not $0 or $202 "
          f"(got {page['m_teaser']['price']})")

    # -- 7. reply linkage rides the field the UI actually reads ------------
    # OFMessage declares `replyToMessageId` (app/lib/relay.ts) and MessageList
    # resolves it through an id map. of_message emitted `replyOnMessageId` —
    # a name nothing in the app reads — so a Fansly quote-reply rendered as a
    # plain bubble with no quoted message above it. It must also be a STRING:
    # two adjacent snowflakes both collapse to the same JS float, so a
    # Number-keyed lookup resolves to the WRONG original.
    from fansly_shim import of_message
    reply_to = "951211561790234625"
    r = of_message({"id": "9", "senderId": US, "content": "", "createdAt": 1.0,
                    "attachments": [], "inReplyTo": reply_to}, account_id=US)
    check("replyToMessageId" in r,
          "of_message emits replyToMessageId (the name OFMessage declares)")
    check("replyOnMessageId" not in r,
          "the old replyOnMessageId name is gone — nothing in the app read it")
    check(r.get("replyToMessageId") == reply_to,
          f"reply id is the exact snowflake STRING (got {r.get('replyToMessageId')!r})")
    check(isinstance(r.get("replyToMessageId"), str), "reply id is a str, not an int")
    # the collapse this guards against, asserted rather than asserted-about
    check(float(reply_to) == float("951211561790234626"),
          "two adjacent snowflakes DO collapse under float — the reason the "
          "MessageList id map had to become string-keyed")
    plain = of_message({"id": "9", "senderId": US, "content": "", "createdAt": 1.0,
                        "attachments": []}, account_id=US)
    check(plain.get("replyToMessageId") is None,
          "a non-reply carries a null reply id, not a stray value")

    # -- 4c. a dict-shaped vault id attaches the SAME grant as a bare id -----
    c, calls = _client()
    c.send_message(GROUP, text="", media_files=[{"mediaId": MEDIA}])
    grants = [b[0] for _, p_, b in calls if p_ == "/account/media"]
    check(len(grants) == 1 and grants[0]["mediaId"] == MEDIA,
          "a {mediaId} dict mints one grant for exactly that vault id")

    # -- 6. upload_media: the 4-step mediav2 flow, field by field ----------
    # Ground truth is capture/RECIPES.md "MEDIA UPLOAD" (run-20260901-2049).
    # Nothing here touches the network: _request records and answers from the
    # capture, http.put is a stub that hands back S3's quoted ETag.
    import tempfile
    from fansly_client import MEDIA_BASE
    UPLOAD_ID = "951251936026308608"
    VAULT_ID = "951251952321183744"
    S3_ETAG = '"93bf238fb9eff41e29ff1f73af3f5e60"'

    def _upload_client(*, part_size: int, n_parts: int, statuses: list[int]):
        c, calls = _client()
        puts: list[tuple[str, bytes, dict]] = []
        polls = iter(statuses)

        def fake(method, path, *, params=None, json_body=None, base=None, **_kw):
            calls.append((method, path, json_body, base))
            if path == "/media/upload/create":
                return {"id": UPLOAD_ID, "type": 1, "partSize": part_size,
                        "status": 2, "mimeType": json_body["mimeType"],
                        "parts": [{"index": i, "uploadUrl": f"https://s3/{i}"}
                                  for i in range(n_parts)]}
            if path == "/media/upload/complete":
                return {"id": UPLOAD_ID, "status": 3, "mediaId": None}
            if path == f"/media/upload/{UPLOAD_ID}":
                st = next(polls)
                return {"id": UPLOAD_ID, "status": st, "mimeType": "image/png",
                        "bucketKey": f"{US}/{UPLOAD_ID}",
                        "mediaId": VAULT_ID if st == 6 else None}
            if path == "/media" and method == "GET":
                # transcoded: PNG in, JPEG stored
                return [{"id": VAULT_ID, "type": 1, "status": 1,
                         "mimetype": "image/jpeg", "width": 4, "height": 4,
                         "locations": [], "variants": []}]
            raise AssertionError(f"unexpected call {method} {path}")

        class _Resp:
            ok = True
            status_code = 200
            text = ""
            headers = {"ETag": S3_ETAG}

        lock = threading.Lock()

        def put(url, data=None, headers=None, timeout=None):
            with lock:              # the parts go up concurrently now
                puts.append((url, data, headers or {}))
            return _Resp()

        c._request = fake
        # The seam is the per-part session, not c.http: each part worker opens
        # its own, because one requests.Session cannot be driven from several
        # threads at once.
        c._upload_session = _session_doing(put)

        def _no(*_a, **_kw):
            raise AssertionError(
                "a part went through the client's SHARED http session")
        c.http.put = _no
        return c, calls, puts

    with tempfile.TemporaryDirectory() as td:
        img = Path(td) / "zz-test.png"
        img.write_bytes(b"\x89PNG" + b"\0" * 175)         # 179 bytes, like the capture
        c, calls, puts = _upload_client(part_size=20971520, n_parts=1,
                                        statuses=[3, 4, 5, 6])
        res = c.upload_media(img, poll_interval_s=0)

        # step 1 — create: body matches the capture field for field
        create = next(b for m, p_, b, base in calls if p_ == "/media/upload/create")
        check(all(base == MEDIA_BASE for _, p_, _, base in calls
                  if p_.startswith("/media/upload")),
              "every upload step goes to mediav2, not apiv3")
        check(create == {
            "fileSize": 179, "mimeType": "image/png", "fileName": "zz-test.png",
            "uploadFormData": {"formInputs": [
                {"type": 1001, "value": "false"}, {"type": 1002, "value": "false"},
                {"type": 1003, "value": ""}, {"type": 1004, "value": ""},
                {"type": 1005, "value": '""'}]}},
            f"upload/create body matches the capture (got {create})")
        # step 2 — one presigned PUT carrying the raw bytes, no Fansly auth
        check(len(puts) == 1 and puts[0][0] == "https://s3/0"
              and puts[0][1] == img.read_bytes(),
              "one part -> one PUT of the whole file to its presigned url")
        check("authorization" not in {k.lower() for k in puts[0][2]}
              and "fansly-client-check" not in {k.lower() for k in puts[0][2]},
              "the S3 PUT carries no Fansly auth (the presign IS the auth)")
        # step 3 — complete echoes the ETag WITH its quotes, by index
        complete = next(b for m, p_, b, _ in calls if p_ == "/media/upload/complete")
        check(complete == {"id": UPLOAD_ID, "type": 1, "partSize": 20971520,
                           "status": 0, "parts": [{"index": 0, "eTag": S3_ETAG}],
                           "waitForComplete": 0},
              f"upload/complete body matches the capture (got {complete})")
        check(complete["parts"][0]["eTag"].startswith('"'),
              "the eTag is echoed quoted, exactly as S3 returned it")
        # step 4 — polled until status 6, then the VAULT id (not the upload id)
        n_polls = sum(1 for _, p_, _, _ in calls if p_ == f"/media/upload/{UPLOAD_ID}")
        check(n_polls == 4, f"polled until status 6 (got {n_polls} polls)")
        check(res["vault_id"] == VAULT_ID and res["send_with"] == [VAULT_ID],
              "returns the assigned vault mediaId, not the upload id")
        check(isinstance(res["vault_id"], str), "vault id is a big-int-safe str")
        check(res["ready"] is True and res["deduped"] is False, "OF result shape")
        check(res["media"].get("id") == VAULT_ID
              and res["stored_mime"] == "image/png",
              "readback carries what the vault stored")
        # the returned send_with feeds straight into the existing grant path
        c2, calls2 = _client()
        c2.send_message(GROUP, text="", media_files=res["send_with"])
        check([b[0]["mediaId"] for _, p_, b in calls2 if p_ == "/account/media"]
              == [VAULT_ID], "upload -> send_with -> grant path, no new logic")

        # multi-part: partSize smaller than the file -> N parts, N PUTs of the
        # right slices, N quoted ETags in index order on complete
        big = Path(td) / "zz-big.bin"
        big.write_bytes(bytes(range(256)) * 10)             # 2560 bytes
        c, calls, puts = _upload_client(part_size=1000, n_parts=3, statuses=[6])
        res = c.upload_media(big, poll_interval_s=0)
        # Parts go up concurrently, so `puts` is in completion order, not part
        # order — index them by their presigned url instead.
        by_url = {u: d for u, d, _h in puts}
        raw = big.read_bytes()
        check(by_url == {"https://s3/0": raw[0:1000],
                         "https://s3/1": raw[1000:2000],
                         "https://s3/2": raw[2000:2560]},
              "a >partSize file is sliced by index into every part")
        complete = next(b for m, p_, b, _ in calls if p_ == "/media/upload/complete")
        check(complete["parts"] == [{"index": i, "eTag": S3_ETAG} for i in range(3)],
              "complete echoes every part's ETag by index")
        # (nothing reached c.http.put — _upload_client wires it to a raiser,
        # because one requests.Session cannot be driven from several threads.)

        # each part is read from the file itself, not from one in-RAM buffer:
        # a worker seeks to its own offset, so peak memory is bounded at
        # concurrency x partSize whatever the file weighs (measured separately
        # at 2 GiB: 80 MiB peak at the default concurrency of 4).
        c, calls, puts = _upload_client(part_size=1000, n_parts=3, statuses=[6])
        c.upload_media(big, poll_interval_s=0)
        check(sum(len(d) for _, d, _h in puts) == big.stat().st_size,
              "the parts together cover the file exactly once")

        # -- a transport blip is retried; a real API answer is NOT ------------
        c, calls, puts = _upload_client(part_size=1000, n_parts=3, statuses=[6])
        blown = {"n": 0}
        real_put = c._upload_session().put

        def _blip_once_on_part_1(url, data=None, headers=None, timeout=None):
            if url.endswith("/1") and blown["n"] < 2:
                blown["n"] += 1
                raise OSError("proxy CONNECT blip")
            return real_put(url, data=data, headers=headers, timeout=timeout)

        c._upload_session = _session_doing(_blip_once_on_part_1)
        res = c.upload_media(big, poll_interval_s=0)
        check(blown["n"] == 2 and res["vault_id"] == VAULT_ID,
              "a transport blip on one part is retried, not fatal")

        c, calls, puts = _upload_client(part_size=1000, n_parts=3, statuses=[6])

        refused: list[str] = []

        def _refuse(url, data=None, headers=None, timeout=None):
            refused.append(url)         # list.append is atomic under the GIL

            class R:
                ok = False
                status_code = 403
                text = "expired presign"
                headers: dict = {}
            return R()

        c._upload_session = _session_doing(_refuse)
        try:
            c.upload_media(big, poll_interval_s=0)
            fails.append("a 403 from S3 must fail the upload, not be swallowed")
        except FanslyAPIError:
            pass
        # EXACTLY 3, not "at most 3": all three parts are submitted to the
        # pool before the first failure surfaces, so each is tried once and
        # only once. `<=` would also pass if a part were silently skipped,
        # which is the more dangerous of the two failures this guards.
        check(len(refused) == 3,
              "a real S3 refusal is re-raised at once, never retried per part "
              f"(got {len(refused)} PUTs for 3 parts)")
        check(not any(p_ == "/media/upload/complete" for _, p_, _, _ in calls),
              "a failed part must never reach upload/complete")
        # a part count that doesn't cover the file must refuse, never truncate
        c, calls, puts = _upload_client(part_size=1000, n_parts=1, statuses=[6])
        try:
            c.upload_media(big, poll_interval_s=0)
            fails.append("a part list that can't hold the file must be refused")
        except FanslyAPIError:
            pass
        check(not puts, "…and refused BEFORE any byte was PUT")
        # a poll that never reaches 6 must not hand back a half-made media
        c, calls, puts = _upload_client(part_size=20971520, n_parts=1,
                                        statuses=[3] * 50)
        try:
            c.upload_media(img, poll_interval_s=0, poll_timeout_s=0)
            fails.append("an upload that never reaches status 6 must raise")
        except FanslyAPIError:
            pass

    for f in fails:
        print(f"  FAIL {f}", file=sys.stderr)
    if fails:
        print(f"{len(fails)} check(s) failed", file=sys.stderr)
        return 1
    print("all send-media checks passed — grant-then-attach matches the capture")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
