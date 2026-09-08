#!/usr/bin/env python3
"""Settle, against a LIVE OnlyFans account, how a fresh upload becomes a vault item.

Everything downstream of this question -- batch upload, Google Drive import, the
UI -- is cheap once it is answered, and unbuildable until it is. Nobody has
answered it: OnlyFans has no "save this upload to my vault" endpoint, and the two
codebases that get media into a vault disagree about the workaround.

    Fastt (here)   attaches the upload claim to a STORY, lets OF file the
                   duplicate, then hides it later. Live-proven daily by
                   automations/auto_stories.py -- but a story is visible to every
                   subscriber for as long as it is up, so it cannot carry a
                   50-file bulk import.

    OnlyStack      POSTs the claim to /chats/{fan}/messages with
                   {isScheduled: true, scheduledDate: +180d}, polls the chat
                   transcript until media[].isReady, keeps media[0].id as the
                   vault id, and deletes the message.
                   Their own plan doc (plans/39) is marked "Implemented (pending
                   live test)" -- it has never been run against production.

The reason that matters: of_client.schedule_message() carries a VERIFIED LIVE
note saying /chats/{id}/messages accepts `scheduledDate` and *sends immediately*
anyway -- real message, real push notification, real fan. OnlyStack additionally
passes `isScheduled: true`, which our test may not have, so this is a genuine
contradiction rather than a settled bug. It is not one to settle by guessing on a
paying subscriber.

So this script walks four probes, cheapest and safest first, and STOPS at the
first one that works:

  shape   Does OF hand back multi-part S3 URLs? One call, uploads no bytes.
          Settles whether upload_media() needs a multipart path at all before
          anyone writes one. Read-only in every sense that matters.

  claim   Upload the bytes and then just WAIT. GET /vault/media/processing
          exists and is documented as "uploads currently being processed", which
          is only a sensible endpoint if a bare claim already enters the vault
          pipeline. If it does, there is no carrier, no trick, no cleanup, and
          three quarters of the planned feature evaporates. Try this first.

  post    Attach the claim to a FAR-FUTURE SCHEDULED POST, poll GET /posts/{id}
          for the media id, then delete the post. Needs no recipient, so it also
          works on an account with zero subscribers, and an unpublished post is
          invisible to fans. This is the carrier to beat.

  queue   POST /messages/queue -- the path this repo verified is genuinely
          deferred -- carrying the claim OBJECT rather than a bare media id.
          Requires --fan-id and writes toward a real person, so it is last and
          gated. OnlyStack's plan 39 flags this same "does /messages/queue take
          the object form?" as unconfirmed.

WRITE SAFETY. `shape` and `claim` only ever add media to your own account.
`post` and `queue` create a real (unpublished, far-future) object on OnlyFans and
need --i-understand-this-writes-to-onlyfans. Every carrier id is appended to a
breadcrumb file BEFORE the carrier is created, so a crash between create and
delete still leaves something to clean up:

    python3 scripts/probe_vault_upload.py --account <id> --cleanup

Cleanup is also what you run if the script ever prints CARRIER LEFT BEHIND. Do
not ignore that line -- a scheduled post that outlives its cleanup publishes the
media to the whole feed on its posted_at date.

Usage (in-container, which is where a captured session lives):

    cat scripts/probe_vault_upload.py | docker exec -i chatterly-relay python3 - \
        --account <account_id> --file /tmp/probe.jpg --probe claim

    ... --probe post --i-understand-this-writes-to-onlyfans
    ... --probe queue --fan-id <burner_fan_id> --i-understand-this-writes-to-onlyfans

Run the whole ladder with --probe all. Point --file at a >5MiB video once the
small-file path is understood; that is what settles whether the md5 dedupe
lookup still finds a multi-part upload (S3's multipart ETag is not the file's
md5, so it may well not).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent / "service"
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

# THE SAME marker and horizon the shipping code uses, imported rather than
# copied. A probe with its own spelling is a carrier that vault_upload.sweep
# cannot find — and a probe carrier is a real scheduled post on a real feed.
# `CARRIER_DAYS` is far enough out that no realistic cleanup delay lets it fire,
# near enough that it is inside whatever scheduling horizon OF enforces.
from of_client import CARRIER_DAYS, VAULT_MARKER as MARKER  # noqa: E402

POLL_INTERVAL_S = 3
POLL_TIMEOUT_S = 300


def _log(msg: str = "") -> None:
    print(msg, flush=True)


def _breadcrumb_path(account_id: str) -> Path:
    return SERVICE_DIR / f".probe_vault_carriers_{account_id}.json"


def _breadcrumb_add(account_id: str, entry: dict) -> None:
    """Record a carrier BEFORE creating it. A crash mid-probe must still leave a
    trail -- the whole point is that an un-deleted scheduled post eventually
    publishes."""
    path = _breadcrumb_path(account_id)
    try:
        existing = json.loads(path.read_text())
    except (OSError, ValueError):
        existing = []
    existing.append(entry)
    path.write_text(json.dumps(existing, indent=2))


def _breadcrumb_clear(account_id: str, kind: str, carrier_id) -> None:
    path = _breadcrumb_path(account_id)
    try:
        existing = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    remaining = [e for e in existing
                 if not (e.get("kind") == kind and str(e.get("id")) == str(carrier_id))]
    path.write_text(json.dumps(remaining, indent=2))


def _file_md5_and_size(path: Path) -> tuple[str, int]:
    """Streamed, so this stays honest on the >5MiB video the probe cares about."""
    h = hashlib.md5()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _future_iso(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


# ── probes ────────────────────────────────────────────────────────────────────

def probe_shape(client, args) -> dict:
    """Ask signed/create for a 3-part upload and look at what comes back.

    Uploads nothing. If the response carries `keys`, upload_media()'s hardcoded
    parts=1 is leaving the multi-part path unimplemented and every large file is
    going up as one enormous PUT.
    """
    _log("── probe: signed/create shape ──")
    key = f"upload/{uuid.uuid4()}/{int(time.time() * 1000)}/probe-shape.bin"
    resp = client.request_signed_upload(key=key, content_type="video/mp4", parts=3)
    keys = resp.get("keys")
    _log(f"  parts=3 -> fields: {sorted(resp.keys())}")
    if isinstance(keys, list) and keys:
        _log(f"  MULTIPART SUPPORTED: {len(keys)} part URLs, uploadId={resp.get('uploadId')!r}")
        _log("  => upload_media() needs the parts/finish path; parts=1 is wrong for big files.")
    else:
        _log(f"  No `keys` returned (putUrl={'yes' if resp.get('putUrl') else 'no'}).")
        _log("  => OF may ignore `parts`; single PUT may be the only path. Verify with a real large file.")
    return {"multipart": bool(keys), "response_fields": sorted(resp.keys())}


def _upload(client, path: Path, args) -> dict:
    _log("── uploading bytes ──")
    md5, size = _file_md5_and_size(path)
    _log(f"  {path.name}  size={size}  md5={md5}")
    existing = client.vault_media_lookup_hash(md5, size)
    if existing:
        _log(f"  DEDUPE HIT: OF already holds these bytes as vault id "
             f"{existing.get('id')}. Use a file OF has never seen, or the probe "
             f"proves nothing about how NEW uploads reach the vault.")
        if not args.allow_dedupe_hit:
            raise SystemExit(2)
    result = client.upload_media(str(path), check_dedupe=False)
    if not result.get("ready"):
        _log(f"  CLAIM FAILED: {result.get('note')}")
        _log("  Bytes are in S3 but convert.onlyfans.com did not claim them.")
        raise SystemExit(3)
    _log(f"  claim ok: send_with={json.dumps(result.get('send_with'))[:300]}")
    result["md5"], result["size"] = md5, size
    return result


def _poll_vault_by_hash(client, md5: str, size: int, *, timeout_s: int) -> dict | None:
    """Wait for OF to file these exact bytes as a vault item.

    Reports /vault/media/processing alongside, because "processing says busy but
    hash never resolves" and "processing is idle and nothing ever appeared" are
    completely different answers.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        hit = client.vault_media_lookup_hash(md5, size)
        if hit:
            return hit
        try:
            proc = client.vault_media_processing()
            busy = proc.get("is_processing") if isinstance(proc, dict) else None
        except Exception as e:  # noqa: BLE001 - diagnostic only
            busy = f"<error {e}>"
        _log(f"  ... not in vault yet (processing={busy}, {int(deadline - time.time())}s left)")
        time.sleep(POLL_INTERVAL_S)
    return None


def probe_claim(client, args) -> dict:
    """Does a bare claim reach the vault with NO carrier at all?

    If yes, the entire scheduled-post/message apparatus is unnecessary and the
    feature is just "upload, then poll".
    """
    up = _upload(client, Path(args.file), args)
    _log(f"── probe: bare claim -> vault (waiting up to {POLL_TIMEOUT_S}s) ──")
    hit = _poll_vault_by_hash(client, up["md5"], up["size"], timeout_s=POLL_TIMEOUT_S)
    if hit:
        _log(f"  ANSWERED: a bare claim DOES land in the vault. id={hit.get('id')}")
        _log("  => No carrier needed. Drop the scheduled-post/message trick entirely.")
        return {"vault_id": hit.get("id"), "carrier_needed": False, "upload": up}
    _log("  A bare claim did NOT reach the vault within the window.")
    _log("  => A carrier is needed. Run --probe post next.")
    return {"vault_id": None, "carrier_needed": True, "upload": up}


def probe_post(client, args, up: dict | None = None) -> dict:
    """Scheduled feed post as the carrier: no recipient, invisible until published."""
    if up is None:
        up = _upload(client, Path(args.file), args)
    send_with = up.get("send_with")
    posted_at = _future_iso(CARRIER_DAYS)
    _log(f"── probe: scheduled post carrier (postedAt={posted_at}) ──")

    _breadcrumb_add(args.account, {"kind": "post", "id": "PENDING", "posted_at": posted_at,
                                   "created": _future_iso(0), "note": "id unknown - create response not seen yet"})
    resp = client.create_post(MARKER, media_files=send_with, posted_at=posted_at, auto_tag=False)
    post_id = resp.get("id") if isinstance(resp, dict) else None
    if post_id is None:
        _log(f"  create_post returned no id: {str(resp)[:400]}")
        _log("  CARRIER LEFT BEHIND (unknown id) -- check your scheduled posts in the OF UI NOW.")
        raise SystemExit(4)
    _breadcrumb_add(args.account, {"kind": "post", "id": post_id, "posted_at": posted_at})
    _log(f"  post id={post_id} created (unpublished). Polling for media ids...")

    vault_ids: list = []
    try:
        deadline = time.time() + POLL_TIMEOUT_S
        while time.time() < deadline:
            detail = client.get_post(post_id)
            media = detail.get("media") or []
            # OF exposes readiness as `isReady` on chat media; on a post it may
            # be absent entirely, in which case an id present at all is the only
            # signal available.
            ready = [m for m in media
                     if m.get("id") is not None and m.get("isReady") is not False]
            if media and len(ready) == len(media):
                vault_ids = [m["id"] for m in ready]
                _log(f"  media ids surfaced on the post: {vault_ids}")
                break
            _log(f"  ... media not resolved yet ({len(media)} entries, "
                 f"{int(deadline - time.time())}s left)")
            time.sleep(POLL_INTERVAL_S)
        else:
            _log("  TIMED OUT waiting for media ids on the post.")
    finally:
        if args.keep:
            # Deliberately left in place so a human can confirm in the OF UI
            # that an unpublished scheduled post really is invisible to fans.
            # The breadcrumb stays, so --cleanup can still remove it later.
            _log(f"── KEEPING carrier post {post_id} (--keep) ──")
            _log(f"  It is scheduled for {posted_at} and WILL PUBLISH then if left.")
            _log(f"  Remove it with:  --account {args.account} --cleanup")
            return {"post_id": post_id, "media_ids": vault_ids, "vault_id": None,
                    "upload": up, "kept": True}
        _log(f"── deleting carrier post {post_id} ──")
        try:
            client.delete_post(post_id)
            _breadcrumb_clear(args.account, "post", post_id)
            _log("  deleted.")
        except Exception as e:  # noqa: BLE001
            _log(f"  CARRIER LEFT BEHIND: delete_post({post_id}) failed: {e}")
            _log(f"  It will PUBLISH on {posted_at}. Re-run with --cleanup, or delete it in the OF UI.")

    _log("── does the vault item survive the carrier's deletion? ──")
    hit = _poll_vault_by_hash(client, up["md5"], up["size"], timeout_s=60)
    if hit:
        _log(f"  YES -- vault id {hit.get('id')} persists after the post was deleted.")
        _log("  => Scheduled post is a viable carrier. Build on this.")
    elif vault_ids:
        _log(f"  Hash lookup missed, but the post exposed ids {vault_ids}.")
        _log("  => Check those ids with vault_media_by_id; if they resolve, poll the")
        _log("     CARRIER for ids rather than the md5 hash (md5 will not match a")
        _log("     multi-part upload's ETag anyway).")
    else:
        _log("  NO -- nothing in the vault. Scheduled post is not a carrier.")
    return {"post_id": post_id, "media_ids": vault_ids,
            "vault_id": hit.get("id") if hit else None, "upload": up}


def probe_queue(client, args, up: dict | None = None) -> dict:
    """/messages/queue -- the path this repo verified as genuinely deferred --
    carrying the claim OBJECT instead of a bare vault id. Writes toward a real
    person, so it is gated behind --fan-id."""
    if not args.fan_id:
        _log("  --probe queue needs --fan-id (use a burner fan account you control).")
        raise SystemExit(5)
    if up is None:
        up = _upload(client, Path(args.file), args)
    scheduled = _future_iso(CARRIER_DAYS)
    _log(f"── probe: /messages/queue carrier (fan={args.fan_id}, at={scheduled}) ──")
    _log("  Watch that fan's inbox in a logged-in browser NOW. If a message appears,")
    _log("  this path sends immediately and must never be used.")

    _breadcrumb_add(args.account, {"kind": "queue", "id": "PENDING", "fan_id": args.fan_id,
                                   "scheduled": scheduled})
    resp = client.schedule_message(args.fan_id, MARKER, scheduled_date=scheduled,
                                   media_files=up.get("send_with"), auto_tag=False)
    queue_id = resp.get("id") if isinstance(resp, dict) else None
    _log(f"  queue entry: {str(resp)[:400]}")
    if queue_id is None:
        _log("  No queue id returned. CHECK THE FAN'S INBOX before doing anything else.")
        raise SystemExit(6)
    _breadcrumb_add(args.account, {"kind": "queue", "id": queue_id, "fan_id": args.fan_id,
                                   "scheduled": scheduled})
    try:
        later = client.schedules_later_chat(limit=20)
        _log(f"  schedules_later_chat sees: {str(later)[:400]}")
    except Exception as e:  # noqa: BLE001
        _log(f"  schedules_later_chat failed: {e}")
    finally:
        _log(f"── cancelling queue entry {queue_id} ──")
        try:
            client.cancel_scheduled(queue_id)
            _breadcrumb_clear(args.account, "queue", queue_id)
            _log("  cancelled.")
        except Exception as e:  # noqa: BLE001
            _log(f"  CARRIER LEFT BEHIND: cancel_scheduled({queue_id}) failed: {e}")
            _log(f"  It fires at {scheduled}. Cancel it in the OF UI.")
    hit = _poll_vault_by_hash(client, up["md5"], up["size"], timeout_s=60)
    _log(f"  vault after queue carrier: {hit.get('id') if hit else 'nothing'}")
    return {"queue_id": queue_id, "vault_id": hit.get("id") if hit else None, "upload": up}


def do_cleanup(client, args) -> dict:
    """Delete every carrier this script recorded and has not confirmed gone."""
    path = _breadcrumb_path(args.account)
    try:
        entries = json.loads(path.read_text())
    except (OSError, ValueError):
        _log(f"No breadcrumbs at {path} -- nothing recorded as outstanding.")
        return {"cleaned": 0}
    if not entries:
        _log("Breadcrumb file is empty -- every carrier was confirmed deleted.")
        return {"cleaned": 0}
    cleaned = 0
    for e in list(entries):
        kind, cid = e.get("kind"), e.get("id")
        if cid in (None, "PENDING"):
            _log(f"  {kind}: id was never recorded (crash between write and create). "
                 f"Check the OF UI for a post/message reading {MARKER!r}.")
            continue
        try:
            if kind == "post":
                client.delete_post(cid)
            elif kind == "queue":
                client.cancel_scheduled(cid)
            else:
                _log(f"  unknown carrier kind {kind!r}, skipping")
                continue
            _breadcrumb_clear(args.account, kind, cid)
            cleaned += 1
            _log(f"  deleted {kind} {cid}")
        except Exception as ex:  # noqa: BLE001
            _log(f"  FAILED to delete {kind} {cid}: {ex} -- do it in the OF UI.")
    return {"cleaned": cleaned}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", required=True, help="Fastt account id (see accounts.list_accounts)")
    p.add_argument("--file", help="media file to upload; use one OF has never seen")
    p.add_argument("--probe", default="claim",
                   choices=["shape", "claim", "post", "queue", "all"])
    p.add_argument("--fan-id", help="burner fan id, required for --probe queue")
    p.add_argument("--cleanup", action="store_true",
                   help="delete carriers recorded in the breadcrumb file, then exit")
    p.add_argument("--keep", action="store_true",
                   help="leave the carrier in place instead of deleting it, so you can "
                        "inspect it in the OF UI; --cleanup removes it later")
    p.add_argument("--allow-dedupe-hit", action="store_true",
                   help="continue even if OF already holds these bytes (results will be muddy)")
    p.add_argument("--i-understand-this-writes-to-onlyfans", action="store_true",
                   dest="confirmed", help="required for the post/queue probes")
    args = p.parse_args(argv)

    import client_pool  # noqa: PLC0415 - needs the sys.path fixup above
    client = client_pool.get(args.account)
    _log(f"account={args.account} of_user_id={client.user_id} rev={client.x_of_rev}")

    if args.cleanup:
        do_cleanup(client, args)
        return 0

    writes = args.probe in ("post", "queue", "all")
    if writes and not args.confirmed:
        _log(f"--probe {args.probe} creates a real (unpublished, +{CARRIER_DAYS}d) object on "
             f"OnlyFans.\nRe-run with --i-understand-this-writes-to-onlyfans.")
        return 1
    if args.probe != "shape" and not args.file:
        _log("--file is required for every probe except `shape`.")
        return 1
    if args.file and not Path(args.file).is_file():
        _log(f"no such file: {args.file}")
        return 1

    if args.probe == "shape":
        probe_shape(client, args)
    elif args.probe == "claim":
        probe_claim(client, args)
    elif args.probe == "post":
        probe_post(client, args)
    elif args.probe == "queue":
        probe_queue(client, args)
    else:
        probe_shape(client, args)
        claim = probe_claim(client, args)
        if not claim["carrier_needed"]:
            _log("\nStopping: a bare claim reaches the vault, so no carrier probe is needed.")
            return 0
        post = probe_post(client, args, up=claim["upload"])
        if post["vault_id"] or post["media_ids"]:
            _log("\nStopping: the scheduled post works as a carrier.")
            return 0
        if args.fan_id:
            probe_queue(client, args, up=claim["upload"])
        else:
            _log("\nPost carrier failed and no --fan-id given, so the queue probe was skipped.")

    left = _breadcrumb_path(args.account)
    if left.exists() and json.loads(left.read_text() or "[]"):
        _log(f"\nCARRIERS STILL RECORDED IN {left} -- run --cleanup.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
