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

The live half — carrier minting, multipart PUTs, dedupe against the real vault
— is exercised by scripts/probe_vault_upload.py against a real session.
"""
import sys
import tempfile
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
vault_upload._running.discard("42")
check("running row + dead process -> interrupted",
      vault_upload._reconcile(running)["status"], "interrupted")
vault_upload._running.add("42")
try:
    check("running row + live process -> running",
          vault_upload._reconcile(running)["status"], "running")
finally:
    vault_upload._running.discard("42")
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


print()
if failures:
    print(f"❌ {len(failures)} FAILED")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("✅ all vault upload tests passed")
