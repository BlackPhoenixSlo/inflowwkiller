"""
Minimal authenticated Fansly client.

The Fansly twin of service/of_client.py, cut down to the calls we have real
captures for: read the account, look a creator up, follow / unfollow, open a DM
group and send a text message.

Deliberately does NOT implement anything that spends money — no purchases, no
tips, no paid-message pricing, no wallet calls. Adding one would need its own
review; this file is for authorized accounts we operate, per BRIEF.md.

    ../venv/bin/python fansly_client.py me
    ../venv/bin/python fansly_client.py lookup <username>
    ../venv/bin/python fansly_client.py follow <accountId>
    ../venv/bin/python fansly_client.py unfollow <accountId>
    ../venv/bin/python fansly_client.py send <accountId|@username> "text"
    ../venv/bin/python fansly_client.py send --group <groupId> "text"

Session material comes from sessions/session.json — see bootstrap_from_har.py
or `--help` below for the one-line browser console snippet that produces it.
Every constant this file relies on is documented in STATIC_VALUES.md.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import requests

import fansly_signer as signer

HERE = Path(__file__).resolve().parent
SESSIONS_DIR = HERE / "sessions"
SESSION_FILE = SESSIONS_DIR / "session.json"

API_BASE = "https://apiv3.fansly.com/api/v1"
# The upload flow lives on a DIFFERENT host (captured: create/complete/poll all
# go to mediav2, the bytes go straight to a presigned S3 url). Same auth, same
# path-only digest, so it is a base-url swap on _request, not a second client.
MEDIA_BASE = "https://mediav2.fansly.com/api/v1"
ORIGIN = "https://fansly.com"
REFERER = "https://fansly.com/"
DEFAULT_TIMEOUT_S = 30

# Angular service-worker bypass — on every call the web client makes.
NGSW_PARAMS = {"ngsw-bypass": "true"}

# Message `type` discriminator. 1 is a plain text message; the paid/media types
# are deliberately not wired up here.
MESSAGE_TYPE_TEXT = 1

# Device ids are re-fetched after this long. The web client holds one much
# longer; refetching per run just draws 429s.
DEVICE_ID_TTL_S = 180 * 60

BROWSER_CONSOLE_SNIPPET = """\
Run this on an authenticated https://fansly.com tab (DevTools console) and save
the output as fansly/sessions/session.json:

  copy(JSON.stringify({
    ...JSON.parse(localStorage.session_active_session),
    device_id: JSON.parse(localStorage.device_device_id || '""'),
    user_agent: navigator.userAgent,
  }, null, 2))

The session blob carries `token` (the authorization header), `id` (the
fansly-session-id header) and `accountId` (us). Nothing else is needed.
"""


class FanslyAPIError(Exception):
    """Non-2xx, or a 200 whose envelope says success:false."""

    def __init__(self, message: str, response: requests.Response | None = None) -> None:
        super().__init__(message)
        self.response = response


class FanslySession:
    """Captured auth material. Field names tolerate both the browser blob's
    camelCase and our own snake_case, so a pasted localStorage dump just works.
    """

    def __init__(self, data: dict[str, Any], path: Path | None = None) -> None:
        self._data = data
        self._path = path

        self.token: str = data.get("token") or ""
        self.session_id: str = data.get("id") or data.get("session_id") or ""
        self.account_id: str = data.get("accountId") or data.get("account_id") or ""
        self.device_id: str = (
            data.get("device_id") or data.get("deviceId") or ""
        )
        self.device_id_ts: float = float(data.get("device_id_ts") or 0)
        self.user_agent: str = data.get("user_agent") or data.get("userAgent") or ""
        self.check_key: str = data.get("check_key") or signer.CHECK_KEY

        # Optional client hints, purely for header fidelity.
        self.accept_language: str = data.get("accept_language") or "en-US,en;q=0.9"
        self.sec_ch_ua: str = data.get("sec_ch_ua") or ""
        self.sec_ch_ua_platform: str = data.get("sec_ch_ua_platform") or ""
        self.sec_ch_ua_mobile: str = data.get("sec_ch_ua_mobile") or "?0"

        if not self.token:
            raise FanslyAPIError(
                "session has no token.\n\n" + BROWSER_CONSOLE_SNIPPET
            )
        if not self.user_agent:
            raise FanslyAPIError(
                "session has no user_agent — it must match the browser that "
                "captured the token.\n\n" + BROWSER_CONSOLE_SNIPPET
            )

    @classmethod
    def load(cls, path: Path = SESSION_FILE) -> "FanslySession":
        if not path.exists():
            raise FanslyAPIError(
                f"no session at {path}.\n\n" + BROWSER_CONSOLE_SNIPPET
            )
        return cls(json.loads(path.read_text()), path)

    def save(self) -> None:
        """Persist refreshed device-id state back to disk."""
        if self._path is None:
            return
        self._data.update(
            {
                "token": self.token,
                "id": self.session_id,
                "accountId": self.account_id,
                "device_id": self.device_id,
                "device_id_ts": self.device_id_ts,
                "user_agent": self.user_agent,
                "check_key": self.check_key,
            }
        )
        self._path.parent.mkdir(exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2) + "\n")
        self._path.chmod(0o600)


class FanslyClient:
    def __init__(
        self,
        session: FanslySession,
        timeout: float = DEFAULT_TIMEOUT_S,
        dry_run: bool = False,
    ) -> None:
        self.session = session
        self.timeout = timeout
        self.dry_run = dry_run
        self.http = requests.Session()
        self.clock = signer.ClientTimestamp()
        self._digests: signer.DigestCache | None = None

    # -- headers ------------------------------------------------------------

    def _digest_cache(self) -> signer.DigestCache:
        """Rebuilt whenever the device id rotates — it is an input to every
        digest, so a stale cache would sign with the old one."""
        if self._digests is None or self._digests.device_id != self.session.device_id:
            self._digests = signer.DigestCache(
                self.session.device_id, self.session.check_key
            )
        return self._digests

    def _headers(self, path: str, *, signed: bool = True, body: bool = False) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": self.session.accept_language,
            "authorization": self.session.token,
            "origin": ORIGIN,
            "referer": REFERER,
            "user-agent": self.session.user_agent,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "priority": "u=1, i",
        }

        if body:
            headers["content-type"] = "application/json"

        for name, value in (
            ("sec-ch-ua", self.session.sec_ch_ua),
            ("sec-ch-ua-platform", self.session.sec_ch_ua_platform),
            ("sec-ch-ua-mobile", self.session.sec_ch_ua_mobile),
        ):
            if value:
                headers[name] = value

        if signed:
            headers["fansly-client-id"] = self.session.device_id
            headers["fansly-client-ts"] = str(self.clock.value())
            headers["fansly-client-check"] = self._digest_cache().get(path)
            if self.session.session_id:
                headers["fansly-session-id"] = self.session.session_id

        return headers

    # -- transport ----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        signed: bool = True,
        base: str | None = None,
    ) -> Any:
        url = f"{base or API_BASE}{path}"
        api_path = f"/api/v1{path}"          # what the digest is computed over

        if self.dry_run and method != "GET":
            print(f"[dry-run] {method} {url}")
            if json_body is not None:
                print(f"[dry-run] body: {json.dumps(json_body)}")
            return None

        response = self.http.request(
            method,
            url,
            params={**NGSW_PARAMS, **(params or {})},
            json=json_body,
            headers=self._headers(api_path, signed=signed, body=json_body is not None),
            timeout=self.timeout,
        )

        try:
            payload = response.json()
        except ValueError:
            raise FanslyAPIError(
                f"{method} {path} -> {response.status_code}, non-JSON body: "
                f"{response.text[:200]}",
                response,
            ) from None

        if not payload.get("success"):
            error = payload.get("error") or {}
            raise FanslyAPIError(
                f"{method} {path} -> {response.status_code} "
                f"code={error.get('code')} {error.get('details')!r}",
                response,
            )

        return payload.get("response")

    # -- bootstrap ----------------------------------------------------------

    def ensure_device_id(self) -> str:
        """Fetch a device id if we have none or ours has aged out.

        `/device/id` is the one call that cannot be signed — the digest needs a
        device id, so this is the bootstrap that produces one.
        """
        fresh = (
            self.session.device_id
            and time.time() - self.session.device_id_ts < DEVICE_ID_TTL_S
        )
        if fresh:
            return self.session.device_id

        device_id = self._request("GET", "/device/id", signed=False)
        self.session.device_id = str(device_id)
        self.session.device_id_ts = time.time()
        self.session.save()
        return self.session.device_id

    # -- reads --------------------------------------------------------------

    def me(self) -> dict:
        return self._request("GET", "/account/me")

    def account_by_username(self, username: str) -> dict:
        accounts = self._request(
            "GET", "/account", params={"usernames": username.lstrip("@")}
        )
        if not accounts:
            raise FanslyAPIError(f"no such account: {username}")
        return accounts[0]

    def resolve_account_id(self, who: str) -> str:
        """Accept either a snowflake id or an @username."""
        if who.startswith("@") or not who.isdigit():
            return self.account_by_username(who)["id"]
        return who

    def groups(self, limit: int = 20, offset: int = 0) -> dict:
        return self._request(
            "GET",
            "/messaging/groups",
            params={
                "sortOrder": "1",
                "flags": "0",
                "subscriptionTierId": "",
                "listIds": "",
                "search": "",
                "limit": str(limit),
                "offset": str(offset),
            },
        )

    # -- follow / unfollow --------------------------------------------------

    def follow(self, account_id: str) -> dict:
        """POST /account/{id}/followers — no body, despite being a POST.

        422 `code=5 'you can not follow'` is Fansly refusing the relationship
        (already following, blocked, self); it is not a signing failure.
        """
        return self._request("POST", f"/account/{account_id}/followers")

    def unfollow(self, account_id: str) -> dict:
        return self._request("POST", f"/account/{account_id}/followers/remove")

    # -- messaging ----------------------------------------------------------

    def find_group_with(self, account_id: str, scan_limit: int = 100) -> str | None:
        """Existing DM group with this account, if the recent list has one."""
        offset = 0
        while offset < scan_limit:
            page = self.groups(limit=20, offset=offset) or {}
            rows = page.get("data") or []
            for row in rows:
                if str(row.get("partnerAccountId")) == str(account_id):
                    return str(row["groupId"])
            if len(rows) < 20:
                return None
            offset += 20
        return None

    def create_group(self, account_id: str) -> str:
        """Open a DM thread. `permissionFlags: 0` on the way in — the server
        returns 65535 for both members."""
        if not self.session.account_id:
            self.session.account_id = str(self.me()["account"]["id"])
            self.session.save()

        created = self._request(
            "POST",
            "/group",
            json_body={
                "users": [
                    {"userId": str(self.session.account_id), "permissionFlags": 0},
                    {"userId": str(account_id), "permissionFlags": 0},
                ],
                "recipients": [],
                "lastMessage": None,
                "userSettings": None,
                "type": 1,
            },
        )
        return str(created["id"]) if created else ""

    def ensure_group(self, account_id: str) -> str:
        return self.find_group_with(account_id) or self.create_group(account_id)

    def typing(self, group_id: str) -> None:
        """The browser fires this before a send. Cheap, and it makes the
        sequence look like a person rather than a bot."""
        self._request("POST", "/message/typing", json_body={"groupId": str(group_id)})

    def send_message(
        self,
        group_id: str,
        text: str,
        *,
        in_reply_to: str | None = None,
        scheduled_for: int | None = 0,
        attachments: list[dict[str, Any]] | None = None,
        broadcast: bool = False,
    ) -> dict:
        """POST /message. `createdAt` is epoch **seconds** with a millisecond
        fraction — not the millisecond integer the rest of the API uses.

        `scheduled_for=None` OMITS the field entirely — the web client includes
        scheduledFor on text sends but omits it on GIF sends (verified in the
        captured HAR).

        `attachments` are the already-minted content envelopes, each
        ``{messageId, pos, contentId, contentType}``. They are NOT raw vault
        media ids: `contentId` is an **accountMedia** id produced by
        POST /account/media (see FanslyShimClient._attach_media). Sending a
        bare vault mediaId here is accepted by the API and silently delivers a
        broken attachment, so mint the envelope first.
        """
        body: dict[str, Any] = {
            "type": MESSAGE_TYPE_TEXT,
            "attachments": list(attachments or []),
            "likes": [],
            "content": text,
            "groupId": str(group_id),
            "inReplyTo": in_reply_to,
            "createdAt": round(time.time(), 3),
        }
        if scheduled_for is not None:
            body["scheduledFor"] = scheduled_for
        # A broadcast is the SAME body on a different path — the web client
        # branches purely on the group's type (3 = broadcast). Posting a
        # broadcast group's message to plain /message is the silent-wrong-path
        # failure to avoid, so the caller states the lane explicitly.
        path = "/message/broadcast" if broadcast else "/message"
        return self._request("POST", path, json_body=body)

    def message_account(self, account_id: str, text: str) -> dict:
        """The whole browser sequence: find-or-create the thread, signal
        typing, send."""
        group_id = self.ensure_group(account_id)
        if self.dry_run and not group_id:
            print(f"[dry-run] would send to account {account_id}: {text!r}")
            return {}
        self.typing(group_id)
        return self.send_message(group_id, text)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _client(args: argparse.Namespace) -> FanslyClient:
    client = FanslyClient(
        FanslySession.load(args.session), dry_run=getattr(args, "dry_run", False)
    )
    client.ensure_device_id()
    return client


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--session", type=Path, default=SESSION_FILE)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the writes instead of making them",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("me", help="GET /account/me — proof of life")

    lookup = sub.add_parser("lookup", help="resolve a username to an account")
    lookup.add_argument("username")

    for name, help_text in (
        ("follow", "follow an account"),
        ("unfollow", "unfollow an account"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("who", help="account id or @username")

    send = sub.add_parser("send", help="send a text message")
    send.add_argument("who", nargs="?", help="account id or @username")
    send.add_argument("text")
    send.add_argument("--group", help="send to an existing groupId instead")

    sub.add_parser("groups", help="list recent message threads")

    args = parser.parse_args(argv)

    try:
        client = _client(args)

        if args.command == "me":
            account = client.me()["account"]
            print(f"{account['username']} ({account['id']})")

        elif args.command == "lookup":
            account = client.account_by_username(args.username)
            print(json.dumps(
                {k: account.get(k) for k in ("id", "username", "displayName", "followCount")},
                indent=2,
            ))

        elif args.command in ("follow", "unfollow"):
            account_id = client.resolve_account_id(args.who)
            result = getattr(client, args.command)(account_id)
            print(f"{args.command} {account_id}: {json.dumps(result)}")

        elif args.command == "send":
            if args.group:
                client.typing(args.group)
                sent = client.send_message(args.group, args.text)
            elif args.who:
                sent = client.message_account(
                    client.resolve_account_id(args.who), args.text
                )
            else:
                parser.error("send needs an account (or --group)")
            if sent:
                print(f"sent {sent['id']} to group {sent['groupId']}")

        elif args.command == "groups":
            for row in (client.groups() or {}).get("data", []):
                print(f"{row['groupId']}  {row.get('partnerUsername')}  "
                      f"unread={row.get('unreadCount')}")

    except FanslyAPIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
