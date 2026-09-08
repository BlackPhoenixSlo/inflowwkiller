#!/usr/bin/env python3
"""Vault batch-upload unit tests — plain asserts, no pytest, no network.

Run: ./venv/bin/python service/tests/test_vault_upload.py

These cover the three pieces where a silent wrong answer is expensive and a
live test is too slow or too destructive to run often:

  * the S3 ETag, because it is OF's dedupe key and is NOT the file md5 once an
    upload goes multipart. Getting it wrong doesn't fail loudly — it just
    re-uploads every large file forever.
  * Drive link parsing, because a dropped `resourcekey` turns a working public
    link into a 404 that reads as "not shared".
  * the run-state reconciler, because a run whose process died must report
    `interrupted`, never a permanent `running` that locks the account out.
  * the carrier INTENT record, because it is written before the create call and
    is the only thing that can find a carrier whose response was lost.
  * a dedupe hit on a HIDDEN vault item, because reporting it as done is how an
    operator loses media that no re-upload can ever restore.
  * the Drive API key never reaching a stored error string.
  * the size limits, because an override that stops being honoured halfway
    through the pipeline fails silently and expensively.
  * the intent hook's FAILURE, because swallowing it and posting anyway is the
    unrecoverable case the intent record exists to prevent.
  * the sweep's liveness rule, because deleting a carrier belonging to a run
    that is still polling fails an item whose media is already on OnlyFans —
    and writing the sweep's stale snapshot back loses everything that run wrote.
  * the import lock, because a pid alone is not an identity: container pids are
    recycled, and "locked out forever" is the failure the lock must not cause.
  * the write gate, because it is what keeps a carrier out of the slot an
    automation's fan message reserved.

The live half — carrier minting, multipart PUTs, dedupe against the real vault
— is exercised by scripts/probe_vault_upload.py against a real session.
"""
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gdrive              # noqa: E402
import of_client           # noqa: E402
import vault_upload        # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def check_raises(label: str, fn, exc) -> None:
    try:
        fn()
    except exc:
        print(f"  ok   {label}")
        return
    except Exception as e:  # noqa: BLE001
        failures.append(f"{label}: raised {type(e).__name__}, want {exc.__name__}")
        print(f"  FAIL {label}: raised {type(e).__name__}, want {exc.__name__}")
        return
    failures.append(f"{label}: did not raise {exc.__name__}")
    print(f"  FAIL {label}: did not raise")


# ── S3 ETag / dedupe key ──────────────────────────────────────────────────
print("\ns3_etag_and_md5")
import hashlib

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    part = 1024                       # tiny parts so the test stays in memory

    one = td / "one.bin"
    one.write_bytes(b"a" * 500)
    key, md5, size = of_client.s3_etag_and_md5(one, part_size=part)
    check("single part: key is the plain md5", key, hashlib.md5(b"a" * 500).hexdigest())
    check("single part: md5 matches key", md5, key)
    check("single part: size", size, 500)

    # Exactly one part is still SINGLE-part — S3 only switches format once a
    # second part exists. An off-by-one here silently breaks 5 MiB files.
    exact = td / "exact.bin"
    exact.write_bytes(b"b" * part)
    key, _, size = of_client.s3_etag_and_md5(exact, part_size=part)
    check("exactly one part: no -N suffix", "-" in key, False)
    check("exactly one part: size", size, part)

    over = td / "over.bin"
    over.write_bytes(b"c" * (part + 1))
    key, md5, size = of_client.s3_etag_and_md5(over, part_size=part)
    expect = (hashlib.md5(
        hashlib.md5(b"c" * part).digest() + hashlib.md5(b"c").digest()
    ).hexdigest() + "-2")
    check("two parts: composite etag", key, expect)
    check("two parts: md5 differs from key", md5 != key, True)
    check("two parts: md5 is still the whole-file digest",
          md5, hashlib.md5(b"c" * (part + 1)).hexdigest())
    check("two parts: size", size, part + 1)

    three = td / "three.bin"
    three.write_bytes(b"d" * (part * 2 + 7))
    key, _, _ = of_client.s3_etag_and_md5(three, part_size=part)
    check("three parts: suffix counts parts", key.rsplit("-", 1)[1], "3")

    empty = td / "empty.bin"
    empty.write_bytes(b"")
    key, md5, size = of_client.s3_etag_and_md5(empty, part_size=part)
    check("empty file: md5 of nothing", key, hashlib.md5(b"").hexdigest())
    check("empty file: size", size, 0)


# ── Google Drive link parsing ─────────────────────────────────────────────
print("\ngdrive.parse_link")
cases = [
    ("https://drive.google.com/file/d/1AbC_dEf/view?usp=sharing", "file", "1AbC_dEf", None),
    ("https://drive.google.com/drive/folders/1Fold_er?usp=drive_link", "folder", "1Fold_er", None),
    ("https://drive.google.com/open?id=1Open_id", "unknown", "1Open_id", None),
    ("https://drive.google.com/uc?export=download&id=1Uc_id&resourcekey=0-AbC",
     "unknown", "1Uc_id", "0-AbC"),
    ("https://docs.google.com/document/d/1Doc_id/edit", "unknown", "1Doc_id", None),
    ("1BareIdLooksLikeThis", "unknown", "1BareIdLooksLikeThis", None),
    # resourcekey must survive on the /file/d/ shape too, not just ?id=
    ("https://drive.google.com/file/d/1Keyed/view?resourcekey=0-XyZ", "file", "1Keyed", "0-XyZ"),
]
for link, kind, fid, rkey in cases:
    ref = gdrive.parse_link(link)
    check(f"kind  {link[:46]:46}", ref.kind, kind)
    check(f"id    {link[:46]:46}", ref.id, fid)
    check(f"rkey  {link[:46]:46}", ref.resource_key, rkey)

for bad in ("", "   ", "https://example.com/file/d/x", "https://drive.google.com/"):
    check_raises(f"rejects {bad!r}", lambda b=bad: gdrive.parse_link(b), gdrive.GDriveError)

print("\ngdrive.is_uploadable")
F = gdrive.DriveFile
for name, mime, ok in (("a.jpg", "image/jpeg", True), ("v.mp4", "video/mp4", True),
                       ("s.mp3", "audio/mpeg", True), ("d.pdf", "application/pdf", False),
                       ("sheet", "application/vnd.google-apps.spreadsheet", False),
                       ("dir", gdrive.FOLDER_MIME, False)):
    got, _ = gdrive.is_uploadable(F("i", name, mime, 1))
    check(f"{name} ({mime})", got, ok)

print("\ngdrive._headers")
check("no resourcekey -> no header", gdrive._headers(gdrive.DriveRef("a", "file")), {})
check("resourcekey -> joined header",
      gdrive._headers(gdrive.DriveRef("a", "file", "k1")),
      {"X-Goog-Drive-Resource-Keys": "a/k1"})


# ── run-state reconciliation ──────────────────────────────────────────────
print("\nvault_upload._reconcile")
running = {"run_id": "r", "account_id": "42", "status": "running"}
vault_upload._live.pop("42", None)
check("running row + dead process -> interrupted",
      vault_upload._reconcile(running)["status"], "interrupted")
vault_upload._live["42"] = "r"
try:
    check("running row + live process -> running",
          vault_upload._reconcile(running)["status"], "running")
    # Liveness is per RUN: an older run of an account that is busy with a NEW
    # import has not somehow come back to life.
    vault_upload._live["42"] = "r2"
    check("older run + account busy with a newer one -> interrupted",
          vault_upload._reconcile(running)["status"], "interrupted")
finally:
    vault_upload._live.pop("42", None)
check("finished row is untouched",
      vault_upload._reconcile({"status": "done", "account_id": "42"})["status"], "done")

print("\nvault_upload._item")
it = vault_upload._item("x.jpg", "/tmp/x.jpg", "local", 10)
check("starts pending", it["status"], "pending")
check("no carrier yet", it["carrier_post_id"], None)
check("carrier_deleted starts unknown, not False", it["carrier_deleted"], None)

print("\ncaps are readable from the environment")
check("MAX_FILES is an int", isinstance(vault_upload.MAX_FILES, int), True)
check("MAX_BYTES tracks MAX_MB", vault_upload.MAX_BYTES, vault_upload.MAX_MB * 1024 * 1024)

# ── the carrier intent record (the worst failure this feature can cause) ──
# A carrier post that outlives its run publishes to the creator's whole feed.
# The dangerous case is not "we forgot to delete it" — it is "the create call's
# response never came back, so no id was ever learned". The ONLY thing that can
# find such a post is a record written before the POST went out.
print("\nof_client.materialize_to_vault: the intent record precedes the POST")


class _Recorder:
    """A stand-in `self` for the unbound method — no session, no network."""
    _VAULT_MARKER = of_client.VAULT_MARKER
    timeout_s = 30

    def __init__(self, create_raises=None):
        self.order: list[str] = []
        self.deleted: list = []
        self._create_raises = create_raises

    def create_post(self, text, **kw):
        self.order.append(f"create:{text}")
        if self._create_raises:
            raise self._create_raises
        return {"id": 9001}

    def get_post(self, post_id):
        return {"media": [{"id": 555, "isReady": True}]}

    def delete_post(self, post_id):
        self.order.append("delete")
        self.deleted.append(post_id)
        return {}

    def vault_media_lookup_hash(self, key, size):
        return None


rec = _Recorder()
seen_intent: list = []
res = of_client.OFClient.materialize_to_vault(
    rec, [{"processId": "p"}],
    on_carrier_intent=lambda marker: (seen_intent.append(marker),
                                      rec.order.append("intent")),
    on_carrier=lambda pid: rec.order.append(f"carrier:{pid}"))
check("intent is recorded BEFORE the create call",
      rec.order[:2], ["intent", f"create:{of_client.VAULT_MARKER}"])
check("the intent carries the marker a sweep searches for",
      seen_intent, [of_client.VAULT_MARKER])
check("the id hook still fires straight after the create",
      rec.order[2], "carrier:9001")
check("vault id comes off the carrier", res["vault_id"], 555)
check("the carrier is deleted", rec.deleted, [9001])

# The scenario the record exists for: the POST is sent, the response is lost.
lost = _Recorder(create_raises=TimeoutError("read timed out"))
intents: list = []
try:
    of_client.OFClient.materialize_to_vault(
        lost, [{"processId": "p"}],
        on_carrier_intent=lambda marker: intents.append(marker))
except TimeoutError:
    pass
check("a lost create response still leaves an intent behind",
      intents, [of_client.VAULT_MARKER])

# And the sweep can act on it: a marker-bearing scheduled post whose id no run
# owns is deletable, which is the only handle on a carrier nobody recorded.
print("\nvault_upload._sweep_scheduled")


class _Scheduled:
    def __init__(self, rows, fail=False):
        self.rows = rows
        self.deleted: list = []
        self.fail = fail
        self.offsets: list[int] = []

    def schedules_later_post(self, limit=10, offset=0):
        if self.fail:
            raise RuntimeError("session went stale")
        self.offsets.append(offset)
        return {"list": self.rows[offset:offset + limit]}

    def delete_post(self, pid):
        self.deleted.append(pid)
        return {}


sched = _Scheduled([
    {"id": 1, "rawText": f"<p>{of_client.VAULT_MARKER}</p>"},
    {"id": 2, "rawText": "a real scheduled post the creator wrote"},
    {"id": 3, "text": of_client.VAULT_MARKER},
])
got, lost_count, complete = vault_upload._sweep_scheduled(sched, "42", lambda: {"3"})
check("deletes the unrecorded carrier, markup and all", sched.deleted, [1])
check("leaves the creator's own post alone", 2 in sched.deleted, False)
check("leaves a carrier a run already tracks to that run", 3 in sched.deleted, False)
check("reports what it deleted", (got, lost_count), (1, 0))
check("a listing that ran to the end is a COMPLETE scan", complete, True)

# A queue longer than one page: the carrier is 30 days out, so it is on the
# LAST page. Truncating at page 1 reported "clean" and let it publish.
long_queue = ([{"id": 1000 + n, "rawText": "a real post"} for n in range(250)]
              + [{"id": 7, "rawText": of_client.VAULT_MARKER}])
paged = _Scheduled(long_queue)
_page_orig = vault_upload._SCHEDULE_PAGE
vault_upload._SCHEDULE_PAGE = 100
try:
    got, _, complete = vault_upload._sweep_scheduled(paged, "42", lambda: set())
finally:
    vault_upload._SCHEDULE_PAGE = _page_orig
check("pages past the first 100 to reach a far-future carrier", paged.deleted, [7])
check("and says so", (got, complete), (1, True))
check("walking the pages means walking the offsets", paged.offsets, [0, 100, 200])

# "Could not list" must NEVER read the same as "listed, found nothing" — this
# is the only recovery path an unrecorded carrier has.
broke = _Scheduled([], fail=True)
got, lost_count, complete = vault_upload._sweep_scheduled(broke, "42", lambda: set())
check("a failed listing is an incomplete scan", complete, False)
check("and is COUNTED as a failure, so the relay shouts", lost_count, 1)


# ── a dedupe hit on a HIDDEN vault item is never 'done' ───────────────────
print("\nvault_upload._upload_one: hidden dedupe hit")


class _FakeClient:
    """Whatever `upload_to_vault` was told to return, plus a call log."""
    def __init__(self, result=None, raises=None):
        self.result = result or {}
        self.raises = raises
        self.calls: list = []

    def upload_to_vault(self, path, **kw):
        self.calls.append(path)
        # Faithful to the real thing: a dedupe hit short-circuits BEFORE
        # materialize_to_vault, so the intent hook never fires for one. Only a
        # file that is about to be posted records an intent.
        if kw.get("on_carrier_intent") and not self.result.get("deduped"):
            kw["on_carrier_intent"](of_client.VAULT_MARKER)
        if self.raises:
            raise self.raises
        return self.result


def _run_state(td: Path, run_id: str = "42-20260101T000000-abc123") -> dict:
    vault_upload.SPOOL_ROOT = td
    vault_upload.LOCK_DIR = td / "locks"
    (td / run_id).mkdir(parents=True, exist_ok=True)
    return vault_upload._new_state(run_id, "42", None)


_spool_root_orig = vault_upload.SPOOL_ROOT
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    state = _run_state(td)
    media = td / "photo.jpg"
    media.write_bytes(b"x" * 32)
    item = vault_upload._item("photo.jpg", str(media), "local", 32)
    state["items"] = [item]
    client = _FakeClient({"vault_id": 77, "ready": True, "deduped": True,
                          "hidden": True, "note": "dedupe hit — HIDDEN"})
    vault_upload._upload_one(state, item, client, None, vault_upload.Limits())
    check("a hidden hit is NOT reported done", item["status"], "skipped")
    check("the operator is told why", "HIDDEN" in (item["error"] or ""), True)
    check("the id is not passed off as an import", item["vault_id"], 77)
    check("a dedupe hit records no carrier intent, because it posts nothing",
          item["carrier_intent"], None)

    # A visible hit is still an ordinary success.
    item2 = vault_upload._item("ok.jpg", str(media), "local", 32)
    state["items"] = [item2]
    vault_upload._upload_one(
        state, item2, _FakeClient({"vault_id": 78, "ready": True,
                                   "deduped": True, "hidden": False}),
        None, vault_upload.Limits())
    check("a visible dedupe hit is done", item2["status"], "done")

    # And the intent SURVIVES a lost response, on disk, in the run record.
    # (The traceback that path logs is the point of the test, not a failure.)
    logging.getLogger("of-relay.vault_upload").setLevel(logging.CRITICAL)
    item3 = vault_upload._item("big.mov", str(media), "local", 32)
    state["items"] = [item3]
    vault_upload._upload_one(state, item3,
                             _FakeClient(raises=TimeoutError("read timed out")),
                             None, vault_upload.Limits())
    logging.getLogger("of-relay.vault_upload").setLevel(logging.NOTSET)
    check("a failed upload keeps its carrier intent", bool(item3["carrier_intent"]), True)
    on_disk = vault_upload.read_state(state["run_id"]) or {}
    check("and the intent is on DISK, not just in memory",
          bool((on_disk.get("items") or [{}])[0].get("carrier_intent")), True)

    # ── the max_bytes override is honoured after compression ──────────────
    print("\nvault_upload.Limits: an override reaches the post-compression check")
    small = vault_upload._item("photo.jpg", str(media), "local", 32)
    state["items"] = [small]
    vault_upload._upload_one(state, small, _FakeClient({"vault_id": 1, "ready": True}),
                            None, vault_upload.Limits(max_bytes=16))
    check("32 bytes is refused by a 16-byte ceiling", small["status"], "skipped")
    check("the message quotes the OVERRIDE, not the module constant",
          "over the 0 MB limit" in (small["error"] or ""), True)
    passing = vault_upload._item("photo.jpg", str(media), "local", 32)
    state["items"] = [passing]
    vault_upload._upload_one(state, passing, _FakeClient({"vault_id": 1, "ready": True}),
                            None, vault_upload.Limits(max_bytes=1024))
    check("and the same file passes a 1 KB ceiling", passing["status"], "done")
vault_upload.SPOOL_ROOT = _spool_root_orig

check("the compression target is forced below the upload ceiling",
      vault_upload.Limits(max_bytes=100 * 1024 * 1024,
                          target_bytes=300 * 1024 * 1024).checked().target_bytes
      < 100 * 1024 * 1024, True)
check("a sane pair is left alone",
      vault_upload.Limits(max_bytes=200, target_bytes=100).checked().target_bytes, 100)


# ── caps ─────────────────────────────────────────────────────────────────
print("\nvault_upload._apply_caps")
plan = [vault_upload._item(f"clip{n}.mp4", f"/tmp/clip{n}.mp4", "local",
                           900 * 1024 * 1024) for n in range(3)]
plan += [vault_upload._item(f"p{n}.jpg", f"/tmp/p{n}.jpg", "local", 1000)
         for n in range(3)]
vault_upload._apply_caps(plan, vault_upload.Limits(max_files=4))
# Oversized video is kept (compression is what rescues it) — and therefore
# COUNTS, which is the bug: 3 videos + 3 photos with a cap of 4 is 4, not 6.
kept = [i for i in plan if i["status"] == "pending"]
check("oversized video counts against the batch cap", len(kept), 4)
check("the overflow says why",
      "batch cap" in (plan[-1]["error"] or ""), True)

huge = [vault_upload._item("dump.zip", "/tmp/dump.zip", "local", 900 * 1024 * 1024)]
vault_upload._apply_caps(huge, vault_upload.Limits())
check("a large non-video is skipped, and says it cannot be compressed",
      huge[0]["status"] == "skipped" and "cannot be compressed" in huge[0]["error"],
      True)


# ── the Drive API key never reaches disk or the screen ───────────────────
print("\ngdrive.redact")
_KEY = "AIza-not-a-real-key-0000000000000000000"
_api_key_orig = gdrive.api_key
gdrive.api_key = lambda: _KEY
try:
    leaky = (f"ConnectionError: HTTPSConnectionPool(host='www.googleapis.com') "
             f"url: /drive/v3/files?alt=media&key={_KEY}")
    check("the key is scrubbed out of an error string",
          _KEY in gdrive.redact(leaky), False)
    check("the rest of the message survives",
          "ConnectionError" in gdrive.redact(leaky), True)
    check("the key is sent as a header, never a query parameter",
          gdrive._auth(), {"x-goog-api-key": _KEY})

    # End to end: whatever the download raises, the run record must not carry it.
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        vault_upload.SPOOL_ROOT = td
        _dl = gdrive.download
        gdrive.download = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError(f"proxy died on ...&key={_KEY}"))
        try:
            it = vault_upload._item("v.mp4", None, "gdrive", 10)
            it.update(drive_id="abc", mime_type="video/mp4")
            items = [it]
            vault_upload._fetch_drive(items, td, vault_upload.Limits(),
                                      lambda _items: None)
        finally:
            gdrive.download = _dl
        check("a stored item error never contains the key",
              _KEY in (items[0]["error"] or ""), False)
        check("the failure is still reported", items[0]["status"], "failed")
    vault_upload.SPOOL_ROOT = _spool_root_orig
finally:
    gdrive.api_key = _api_key_orig


# ── run ids are validated before they touch a path ───────────────────────
print("\nvault_upload._run_dir")
for bad in ("../../etc", "..", "run.json", "42-2026-01-01-abc"):
    check_raises(f"rejects {bad!r}", lambda b=bad: vault_upload._run_dir(b), ValueError)
check("accepts a generated id",
      vault_upload._run_dir("42-20260101T000000-abc123").name,
      "42-20260101T000000-abc123")


# ── one predicate for 'can this be compressed?' ──────────────────────────
print("\nmedia_prep.can_shrink")
import media_prep  # noqa: E402
_ffmpeg_orig = media_prep.have_ffmpeg
media_prep.have_ffmpeg = lambda: True
try:
    check("a Drive video mime says yes", media_prep.can_shrink("clip.flv", "video/x-flv"), True)
    check("and the suffix agrees, so a fetched .flv is not then refused",
          media_prep.can_shrink("clip.flv", None), True)
    check("an image is no", media_prep.can_shrink("a.jpg", "image/jpeg"), False)
    check("a local video with no mime falls back to the suffix",
          media_prep.can_shrink("a.mkv"), True)
    check("an unknown local file is no", media_prep.can_shrink("a.zip"), False)
finally:
    media_prep.have_ffmpeg = _ffmpeg_orig
media_prep.have_ffmpeg = lambda: False
try:
    check("without ffmpeg nothing is compressible — so nothing is fetched "
          "under the big download ceiling either",
          media_prep.can_shrink("a.mp4", "video/mp4"), False)
finally:
    media_prep.have_ffmpeg = _ffmpeg_orig


# ── an intent that cannot be RECORDED must stop the carrier being CREATED ──
# The whole feature rests on this. The intent hook writes to the same spool
# volume the import is filling, so its realistic failure is ENOSPC — precisely
# when a carrier is most likely to be created and never recorded. Logging the
# failure and posting anyway is an unrecoverable scheduled post on a real feed.
print("\nof_client.materialize_to_vault: a failed intent hook aborts the POST")

blocked = _Recorder()
check_raises(
    "an intent hook that raises propagates",
    lambda: of_client.OFClient.materialize_to_vault(
        blocked, [{"processId": "p"}],
        on_carrier_intent=lambda marker: (_ for _ in ()).throw(
            OSError("No space left on device"))),
    OSError)
check("and NO carrier post was created", blocked.order, [])

# The id hook is the same argument one step later: if the id cannot be written
# down, the carrier is deleted again rather than left live and untracked.
untracked = _Recorder()
check_raises(
    "an on_carrier hook that raises propagates too",
    lambda: of_client.OFClient.materialize_to_vault(
        untracked, [{"processId": "p"}],
        on_carrier=lambda pid: (_ for _ in ()).throw(OSError("read-only fs"))),
    OSError)
check("and the carrier it could not record is deleted again",
      untracked.deleted, [9001])


# ── every OnlyFans write goes through the injected gate ───────────────────
# The gate is what keeps a carrier out of the slot an automation's fan message
# reserved. Four writes need it, and until now every test passed `None`.
print("\nthe write gate: all four writes, and pacing that is policy not identity")

gated: list[str] = []


def _recording_gate(thunk):
    gated.append("write")
    return thunk()


class _GateClient:
    """Records what it was handed, and leaves a carrier for cleanup to chase."""
    def __init__(self):
        self.gate_seen = "not passed"
        self.writes: list = []

    def upload_to_vault(self, path, **kw):
        self.gate_seen = kw.get("gate")
        kw["on_carrier_intent"](of_client.VAULT_MARKER)
        kw["on_carrier"](9001)
        # carrier_deleted False: the per-file delete failed, so _cleanup_carriers
        # has to retry it — through the gate.
        return {"vault_id": 5, "ready": True, "deduped": False, "hidden": False,
                "carrier_post_id": 9001, "carrier_deleted": False}

    def delete_post(self, pid):
        self.writes.append(("delete", pid))
        return {}

    def add_media_to_vault_list(self, list_id, ids):
        self.writes.append(("file", list_id, tuple(ids)))
        return {}


with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    vault_upload.SPOOL_ROOT = td
    vault_upload.LOCK_DIR = td / "locks"
    media = td / "a.jpg"
    media.write_bytes(b"x" * 8)
    gc_ = _GateClient()
    st = vault_upload.start("42", client=gc_, local_paths=[str(media)],
                            list_id=7, gate=_recording_gate,
                            limits=vault_upload.Limits(pace_s=0))
    check("the run finished", st["status"], "done")
    check("the gate reaches of_client, which owns the carrier POST and DELETE",
          gc_.gate_seen is _recording_gate, True)
    check("the carrier cleanup retry went through the gate",
          ("delete", 9001) in gc_.writes, True)
    check("so did the foldering call", ("file", 7, (5,)) in gc_.writes, True)
    check("and both were counted by the gate", len(gated), 2)

    # Pacing is `limits.pace_s` and nothing else. It used to be "did the caller
    # pass a gate?", so a test double, a metrics wrapper — any gate but the
    # relay's own — silently turned pacing off.
    slept: list[float] = []
    _sleep_orig = vault_upload.time.sleep
    vault_upload.time.sleep = lambda n: slept.append(n)
    try:
        b1, b2 = td / "b1.jpg", td / "b2.jpg"
        b1.write_bytes(b"y" * 8)
        b2.write_bytes(b"z" * 8)
        vault_upload.start("42", client=_GateClient(),
                           local_paths=[str(b1), str(b2)],
                           gate=_recording_gate,
                           limits=vault_upload.Limits(pace_s=3))
    finally:
        vault_upload.time.sleep = _sleep_orig
    check("a gate does not disable pacing — pace_s does", slept, [3])
vault_upload.SPOOL_ROOT = _spool_root_orig
vault_upload.LOCK_DIR = _spool_root_orig / "locks"


# ── a stale lock whose pid was recycled is stale ──────────────────────────
# Container pids are small and recycled. Checking only os.kill(pid, 0) meant a
# relay OOM-killed at pid 37 locked the feature out forever the moment anything
# else was handed pid 37.
print("\nvault_upload._acquire_lock: pid reuse is not liveness")

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    vault_upload.SPOOL_ROOT = td
    vault_upload.LOCK_DIR = td / "locks"
    vault_upload.LOCK_DIR.mkdir(parents=True)
    lock = vault_upload._lock_path("42")
    _start_orig = vault_upload._pid_start
    vault_upload._pid_start = lambda pid: "STARTED-NOW"
    try:
        import json as _json
        # Same pid, DIFFERENT process: the recycled-pid case.
        lock.write_text(_json.dumps({"pid": os.getpid(), "started": "STARTED-LONG-AGO"}))
        check("a live pid that started at another time is a STALE lock",
              vault_upload._lock_holder("42"), None)
        check("so the lock can be taken", vault_upload._acquire_lock("42") == lock, True)

        # Same pid, same start: a genuinely live holder.
        lock.write_text(_json.dumps({"pid": 1, "started": "STARTED-NOW"}))
        check("a live pid with a matching start time IS the holder",
              (vault_upload._lock_holder("42") or {}).get("pid"), 1)
        check_raises("and a second process is refused",
                     lambda: vault_upload._acquire_lock("42"), RuntimeError)
        check("the account reads as busy", vault_upload._account_is_live("42"), True)

        # Nothing can hold a lock for a day.
        old = time.time() - vault_upload._LOCK_MAX_AGE_S - 60
        os.utime(lock, (old, old))
        check("and a lock older than any real import is stale",
              vault_upload._lock_holder("42"), None)
    finally:
        vault_upload._pid_start = _start_orig
vault_upload.SPOOL_ROOT = _spool_root_orig
vault_upload.LOCK_DIR = _spool_root_orig / "locks"


# ── the sweep must never touch a run that is still going ──────────────────
# materialize_to_vault polls for up to 600s per file, and for that whole window
# the item on disk says "carrier 9001, not deleted". The hourly sweep firing
# inside that window used to delete the carrier out from under the poll — and
# then write its own stale snapshot back over run.json.
print("\nvault_upload.sweep: a live run owns its carriers")


class _SweepClient:
    def __init__(self):
        self.deleted: list = []

    def delete_post(self, pid):
        self.deleted.append(pid)
        return {}

    def schedules_later_post(self, limit=10, offset=0):
        return {"list": []}


def _write_run(td: Path, run_id: str, *, status: str, items: list,
               account: str = "42") -> Path:
    d = td / run_id
    d.mkdir(parents=True, exist_ok=True)
    state = vault_upload._new_state(run_id, account, None)
    state.update(status=status, items=items,
                 finished_at=None if status == "running" else vault_upload._now())
    (d / "run.json").write_text(__import__("json").dumps(state))
    return d / "run.json"


with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    vault_upload.SPOOL_ROOT = td
    vault_upload.LOCK_DIR = td / "locks"
    live_id, dead_id = "42-20260101T000000-aaaaaa", "43-20260101T000001-bbbbbb"

    def _carrier(pid):
        it = vault_upload._item("v.mp4", None, "local", 10)
        it.update(carrier_post_id=pid, carrier_deleted=False, status="uploading")
        return it

    live_json = _write_run(td, live_id, status="running", items=[_carrier(1111)])
    # A different account, so this asserts "the sweep still does its job"
    # rather than "the sweep does nothing". Account 42 is deliberately left
    # ENTIRELY alone while it has an importer: an hourly sweep can wait an hour,
    # and guessing which of a busy account's carriers are orphaned is the guess
    # that deletes a live one.
    _write_run(td, dead_id, status="failed", items=[_carrier(2222)],
               account="43")
    before = live_json.read_text()

    vault_upload._live["42"] = live_id
    try:
        client = _SweepClient()
        res = vault_upload.sweep(lambda aid: client)
    finally:
        vault_upload._live.pop("42", None)
    check("the orphan of the finished run is deleted", client.deleted, [2222])
    check("the LIVE run's carrier is left alone", 1111 in client.deleted, False)
    check("and its run.json is not clobbered from a stale snapshot",
          live_json.read_text(), before)
    check("the sweep reports only what it actually did", res["carriers_deleted"], 1)

    # Cross-process: the CLI's `--sweep` runs in a process whose `_live` is
    # empty. The lockfile is the only thing that can say the dashboard's import
    # is running, and without it the sweep deleted live carriers deliberately.
    live_json.write_text(before)
    vault_upload.LOCK_DIR.mkdir(parents=True, exist_ok=True)
    vault_upload._acquire_lock("42")
    try:
        client2 = _SweepClient()
        vault_upload.sweep(lambda aid: client2)
    finally:
        vault_upload._release_lock(vault_upload._lock_path("42"))
    check("an account whose LOCK is held is never swept", client2.deleted, [])
    check("its record is untouched too", live_json.read_text(), before)
vault_upload.SPOOL_ROOT = _spool_root_orig
vault_upload.LOCK_DIR = _spool_root_orig / "locks"


# ── what the operator is warned about ────────────────────────────────────
# Two ways to get this wrong, and the first version managed both: it missed the
# intent-only orphan (the dangerous one — no id exists to name it) and it fired
# on every healthy import, for the second or two a carrier legitimately exists.
print("\nvault_upload.carriers_outstanding")

_dead = vault_upload._item("a.mp4", None, "local", 1)
_dead.update(carrier_post_id=3333, carrier_deleted=False)
_ghost = vault_upload._item("b.mp4", None, "local", 1)
_ghost.update(carrier_intent={"at": "now", "marker": of_client.VAULT_MARKER},
              carrier_deleted=False)
_gone = vault_upload._item("c.mp4", None, "local", 1)
_gone.update(carrier_post_id=4444, carrier_deleted=True)

finished = {"run_id": "42-20260101T000000-ffffff", "account_id": "42",
            "status": "failed", "finished_at": vault_upload._now(),
            "items": [_dead, _ghost, _gone]}
check("a finished run's undeletable carrier is counted",
      vault_upload.carriers_outstanding("42", [finished])[0], 1)
check("so is an intent with no id — the one no id can name",
      vault_upload.carriers_outstanding("42", [finished])[1], 1)
check("a carrier that was deleted is not",
      vault_upload.carriers_outstanding("42", [{**finished, "items": [_gone]}]), (0, 0))

in_flight = {"run_id": "42-20260101T000001-aaaaaa", "account_id": "42",
             "status": "running", "finished_at": None, "items": [_dead, _ghost]}
vault_upload._live["42"] = in_flight["run_id"]
try:
    check("a RUNNING run's carrier is not an escape — it is a normal import",
          vault_upload.carriers_outstanding("42", [in_flight]), (0, 0))
    # The suppression is per RUN, not per account, and `finished_at` is what
    # says so — it is written exactly once, in start()'s finally, so it is the
    # one "this run is over" any process can trust. Inheriting the account's
    # busy flag meant a week-old escaped carrier was reported as clean for as
    # long as ANY import was going — and a wedged run kept `_live` set for the
    # life of the relay, which here is weeks.
    check("but a FINISHED run's carrier stays on screen while a different "
          "run imports", vault_upload.carriers_outstanding("42", [finished]),
          (1, 1))
finally:
    vault_upload._live.pop("42", None)

# A run that died without saying so is not "in flight" — its carrier is exactly
# the escape this warning exists for.
check("an interrupted run's carrier IS an escape",
      vault_upload.carriers_outstanding("42", [vault_upload._reconcile(in_flight)]),
      (1, 1))

# R10: an intent a completed scan cleared is resolved — and it says that in its
# own field. It used to say it in `carrier_deleted`, asserting a deletion that
# never happened on a field the CLI and the dashboard read as "a post we named
# is gone".
_scanned = dict(_ghost, carrier_scan_clear=True)
check("an intent a completed scan cleared is not counted",
      vault_upload.carriers_outstanding("42", [{**finished, "items": [_scanned]}]),
      (0, 0))


# ── liveness is ONE question with ONE answer ─────────────────────────────
# It had three implementations: a named predicate, a character-for-character
# copy inside the warnings, and a narrower `_live`-only rule in the reconciler.
# One HTTP response could call a run "interrupted" — rendered as "the relay
# restarted mid-run" — while suppressing its carrier warnings BECAUSE the
# account was busy. Two answers to one question, in one dict.
print("\nvault_upload._run_is_live: one predicate, every caller")

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    vault_upload.SPOOL_ROOT = td
    vault_upload.LOCK_DIR = td / "locks"
    vault_upload.LOCK_DIR.mkdir(parents=True)
    cli_run = "42-20260101T000000-cccccc"
    _write_run(td, cli_run, status="running", items=[])
    state = vault_upload.read_state(cli_run) or {}
    check("no claim anywhere -> interrupted", state.get("status"), "interrupted")

    # A CLI import in ANOTHER process: `_live` here is empty and the lockfile is
    # the only witness. Reporting that run as "the relay restarted mid-run" was
    # the visible half of the split predicate.
    lock = vault_upload._lock_path("42")
    lock.write_text(json.dumps({"pid": os.getpid(),
                                "started": vault_upload._pid_start(os.getpid()),
                                "instance": "some-other-process",
                                "run_id": cli_run}))
    check("a run another process holds the lock for is LIVE, not interrupted",
          (vault_upload.read_state(cli_run) or {}).get("status"), "running")
    # And still per-run: the lock names one run, not the account.
    other = "42-20260101T000009-dddddd"
    _write_run(td, other, status="running", items=[])
    check("a DIFFERENT run of the same account is not live off that lock",
          (vault_upload.read_state(other) or {}).get("status"), "interrupted")
    lock.unlink()

    # The in-process claim has the ceiling the lockfile always had. A wedged run
    # used to keep `_live` set for the life of the relay — weeks — and while it
    # was set the sweep skipped every run dir of that account and the operator
    # was told the carriers were clean.
    vault_upload._live["42"] = cli_run
    try:
        check("a live claim with a fresh heartbeat is live",
              vault_upload._account_is_live("42"), True)
        old = time.time() - vault_upload._LIVE_MAX_STALL_S - 60
        os.utime(vault_upload._state_path(cli_run), (old, old))
        check("a claim whose run stopped writing hours ago is not",
              vault_upload._account_is_live("42"), False)
    finally:
        vault_upload._live.pop("42", None)
vault_upload.SPOOL_ROOT = _spool_root_orig
vault_upload.LOCK_DIR = _spool_root_orig / "locks"


# ── the marker scan re-asks; pass 1's answers go stale ───────────────────
# Pass 2 pages OnlyFans — up to fifty sequential round trips — and deletes any
# marker-bearing post it cannot find in the set of ids it knows. Both that set
# and "is this account importing?" were snapshots taken before the paging
# started, so a carrier created DURING the scan was, by construction, unknown.
print("\nvault_upload.sweep: pass 2 re-asks before every delete")


class _MidScan:
    """Answers the listing, and starts an import while it does."""

    def __init__(self, td, on_list):
        self.td, self.on_list = td, on_list
        self.deleted: list = []
        self.listed = 0

    def schedules_later_post(self, limit=100, offset=0):
        if offset:
            return {"list": []}
        self.listed += 1
        self.on_list()          # ... an import starts, right here
        return {"list": [{"id": 5555, "rawText": of_client.VAULT_MARKER}]}

    def delete_post(self, pid):
        self.deleted.append(pid)
        return {}


def _ghost_run(td, run_id, account="42"):
    """A finished run holding an intent and NO id — the record that puts an
    account into the marker scan in the first place."""
    it = vault_upload._item("v.mp4", None, "local", 10)
    it.update(carrier_intent={"at": vault_upload._now(),
                              "marker": of_client.VAULT_MARKER},
              status="failed")
    return _write_run(td, run_id, status="failed", items=[it], account=account)

# (a) the new carrier is RECORDED mid-scan: re-reading the run dirs is what
#     makes it known, and a known id is never touched.
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    vault_upload.SPOOL_ROOT = td
    vault_upload.LOCK_DIR = td / "locks"
    ghost = _ghost_run(td, "42-20260101T000000-eeeeee")
    new_run = "42-20260102T000000-111111"

    def _start_recording():
        it = vault_upload._item("w.mp4", None, "local", 10)
        it.update(carrier_post_id=5555, carrier_deleted=False, status="uploading")
        _write_run(td, new_run, status="failed", items=[it])

    client = _MidScan(td, _start_recording)
    res = vault_upload.sweep(lambda aid: client)
    check("a carrier recorded AFTER pass 1 is not an unknown carrier",
          client.deleted, [])
    check("and the listing did happen — this is not a vacuous pass",
          client.listed, 1)

# (b) the account goes LIVE mid-scan: the scan is abandoned, and an abandoned
#     scan retires nothing.
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    vault_upload.SPOOL_ROOT = td
    vault_upload.LOCK_DIR = td / "locks"
    ghost = _ghost_run(td, "42-20260101T000000-eeeeee")
    live_run = "42-20260102T000000-222222"

    def _go_live():
        _write_run(td, live_run, status="running", items=[])
        vault_upload._live["42"] = live_run

    client = _MidScan(td, _go_live)
    try:
        vault_upload.sweep(lambda aid: client)
    finally:
        vault_upload._live.pop("42", None)
    check("a carrier of a run that went live mid-scan is left alone",
          client.deleted, [])
    on_disk = json.loads(ghost.read_text())
    check("and an abandoned scan retires no intent",
          bool((on_disk["items"][0]).get("carrier_scan_clear")), False)
vault_upload.SPOOL_ROOT = _spool_root_orig
vault_upload.LOCK_DIR = _spool_root_orig / "locks"


# ── a carrier OnlyFans says is not there cannot publish ──────────────────
# `materialize_to_vault` reports `carrier_deleted` through its RETURN value, so
# anything raising between the id and the return — one non-2xx from a 600s poll
# — propagated past a delete that SUCCEEDED. The item kept "carrier live, never
# deleted" forever, every retry 404'd, and the red banner could never clear.
print("\nof_client.post_is_gone: a 404 on the delete is the delete working")


class _Resp:
    def __init__(self, code):
        self.status_code = code


check("404 means gone",
      of_client.post_is_gone(of_client.OFAPIError("404 for x", response=_Resp(404))), True)
check("410 too",
      of_client.post_is_gone(of_client.OFAPIError("410 for x", response=_Resp(410))), True)
check("401 does NOT — a stale session hides a live post",
      of_client.post_is_gone(of_client.OFAPIError("401 for x", response=_Resp(401))), False)
check("and a response-less error is read off the message",
      of_client.post_is_gone(of_client.OFAPIError("404 for x")), True)


class _Gone:
    def __init__(self):
        self.tried: list = []

    def delete_post(self, pid):
        self.tried.append(pid)
        raise of_client.OFAPIError("404 for /posts", response=_Resp(404))


with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    vault_upload.SPOOL_ROOT = td
    stuck = vault_upload._item("a.mp4", None, "local", 1)
    stuck.update(carrier_post_id=9001, carrier_deleted=False)
    st = {"run_id": "42-20260101T000000-abc123", "items": [stuck]}
    gone = _Gone()
    vault_upload._cleanup_carriers(st, gone)
    check("a 404 on the retry resolves the carrier, it does not alarm forever",
          stuck["carrier_deleted"], True)
    check("and it was actually attempted", gone.tried, [9001])
vault_upload.SPOOL_ROOT = _spool_root_orig

# The hook half of the same fix: the record is written from of_client's own
# `finally`, so it is right even when the call is on its way out with an error.
class _PollDies(_Recorder):
    def get_post(self, post_id):
        raise of_client.OFAPIError("500 for /posts", response=_Resp(500))


dying = _PollDies()
recorded: list = []
try:
    of_client.OFClient.materialize_to_vault(
        dying, [{"processId": "p"}], timeout_s=1, poll_interval_s=0,
        on_carrier=lambda pid: None,
        on_carrier_deleted=lambda pid: recorded.append(pid))
except of_client.OFAPIError:
    pass
check("the delete still happened", dying.deleted, [9001])
check("and it was RECORDED, though the body raised past the return",
      recorded, [9001])


# ── releasing a lock must not release somebody else's ────────────────────
# `start()` pops its `_live` claim and releases the lock as two statements. A
# second `begin()` landing in between takes a fresh lock — and the first run's
# release then deleted the NEW run's file, silently turning cross-process
# exclusion off for the run that was actually going.
print("\nvault_upload._release_lock: only ever our own")

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    vault_upload.SPOOL_ROOT = td
    vault_upload.LOCK_DIR = td / "locks"
    old_run, new_run = "42-20260101T000000-aaa111", "42-20260101T000100-bbb222"
    p = vault_upload._acquire_lock("42", old_run)
    p.unlink()
    vault_upload._acquire_lock("42", new_run)          # the newer run's lock
    vault_upload._release_lock(p, old_run)             # the older run finishing
    check("the newer run's lockfile survives the older run's release",
          p.exists(), True)
    check("and it still names the newer run",
          vault_upload._read_lock(p).get("run_id"), new_run)
    vault_upload._release_lock(p, new_run)
    check("its own release does remove it", p.exists(), False)

    # A lock written by a process we cannot see is UNKNOWN, not stale. Reading
    # "the start times differ" as death across a pid namespace turned G3's
    # lockout into silent lock-stealing: two importers on one account.
    p.write_text(json.dumps({"pid": os.getpid(), "started": "STARTED-ELSEWHERE",
                             "instance": "another-container", "run_id": "x"}))
    check("a foreign instance with a mismatched start is still a holder",
          (vault_upload._lock_holder("42") or {}).get("instance"), "another-container")
    check_raises("so we refuse rather than steal the lock",
                 lambda: vault_upload._acquire_lock("42", new_run), RuntimeError)
vault_upload.SPOOL_ROOT = _spool_root_orig
vault_upload.LOCK_DIR = _spool_root_orig / "locks"


# ── the write gate bounds the WAIT; the write itself outlives it ──────────────
# `run_coroutine_threadsafe` hands back a future whose timeout does not touch
# the coroutine, so a timed-out carrier POST is still queued and still fires
# when its pacing slot arrives — after the run ended and after a marker scan
# has correctly found nothing. Retiring that intent is how a post nobody is
# tracking reaches the creator's feed thirty days later.
print("\nvault_upload: an intent whose write may still fire is never retired")

_pending = {"carrier_intent": "MARKER", "carrier_post_id": None,
            "carrier_scan_clear": True, "carrier_write_pending": True}
check("a completed scan does NOT resolve it",
      vault_upload._intent_unresolved(_pending), True)
check("and it keeps the run record alive for the next sweep",
      vault_upload._intent_unresolved({**_pending, "carrier_scan_clear": True}), True)

# The counterpart: the ordinary successful file. An intent is written before
# EVERY carrier POST and is never cleared once an id is learned, so spelling
# this without the id check put every account that had ever imported into the
# hourly scan — a full paged walk of the creator's schedule, for nothing.
_done = {"carrier_intent": "MARKER", "carrier_post_id": 99,
         "carrier_deleted": True, "carrier_scan_clear": None}
check("a successfully imported file leaves nothing outstanding",
      vault_upload._intent_unresolved(_done), False)
check("an id-less intent with no scan yet IS outstanding",
      vault_upload._intent_unresolved({"carrier_intent": "MARKER"}), True)
check("and a completed scan resolves that one",
      vault_upload._intent_unresolved(
          {"carrier_intent": "MARKER", "carrier_scan_clear": True}), False)

# One definition, four askers. This is the rule that has now been spelled by
# hand in four places and disagreed in two of them.
_src = (Path(__file__).resolve().parents[1] / "vault_upload.py").read_text()
check("no hand-rolled copy of the rule survives",
      _src.count('not i.get("carrier_scan_clear")')
      + _src.count('not it.get("carrier_scan_clear")'), 0)

# begin() is the only gate. The cheap in-process pre-check could not see the
# cross-process lock, so a multi-gigabyte drag-in was staged to disk in full
# before the lock refused it — and once a wedged run aged past the stall
# ceiling it answered 409 to the very re-run the dashboard was advising.
check("is_running is gone", hasattr(vault_upload, "is_running"), False)


print()
if failures:
    print(f"❌ {len(failures)} FAILED")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("✅ all vault upload tests passed")
