"""Batch-upload media into the OnlyFans vault, from disk or Google Drive.

THE SHAPE OF THE PROBLEM. OnlyFans has no "save to vault" endpoint (see
of_client.materialize_to_vault for the live evidence). Media enters the vault
only when something references it, so every fresh file costs a carrier post
that we create, read a vault id off, and delete. That makes a bulk import a
long sequence of small, individually-failable, individually-DANGEROUS steps:
a carrier that outlives its run is a scheduled post that eventually publishes
to the creator's whole feed.

So this module is built around two rules the reference implementation breaks:

  1. **The carrier id is written to disk BEFORE the carrier exists**, and the
     record survives the process. `of_client` holds it on the stack and deletes
     it in a `finally`, which a SIGKILL does not run. Here, `sweep()` on boot
     finds and deletes anything an earlier crash left behind.
  2. **The spool is the run's own responsibility.** Files land in
     `service/vault_spool/<run_id>/`, are deleted as each one succeeds, and the
     directory goes when the run ends — including when it ends badly. A VPS
     that fills its disk with abandoned gigabytes takes SQLite down with it.

FILES are processed one at a time on purpose, and that is not a bandwidth
decision — OnlyFans throttles post creation to about one per ten seconds, and
every file needs one carrier post, so parallel files would only queue up against
that limit while multiplying the number of live carriers at any instant. (The
PARTS of a single file do upload concurrently; see of_client._PART_CONCURRENCY,
measured at 2x on a home connection.)

State lives in `<spool>/<run_id>/run.json` rather than a DB table: it must be
readable by a sweep that runs before anything else boots, it is per-run garbage
that should die with its directory, and `atomic_json` already gives us a
never-half-written small file.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import atomic_json
import media_prep

log = logging.getLogger("of-relay.vault_upload")

HERE = Path(__file__).resolve().parent
SPOOL_ROOT = Path(os.environ.get("VAULT_SPOOL_DIR") or (HERE / "vault_spool"))

# Caps. Per-file size is the one an operator actually tunes; the batch cap
# exists because every file costs a carrier post, and a runaway batch is a
# runaway number of scheduled posts on a real feed.
MAX_FILES = int(os.environ.get("VAULT_UPLOAD_MAX_FILES") or 40)
# MEASURED CEILING. `convert.onlyfans.com` registers a 157 MB object fine (198s
# end to end) and answers a hard 504 Gateway Time-out on a 443 MB one — three
# claim attempts spread over 80s, all 504, so it is a size limit and not
# congestion. The exact cutoff is somewhere in between and untested; 200 MB is
# the conservative side of it. Video above VAULT_COMPRESS_OVER_MB is re-encoded
# before it ever reaches this check (see media_prep), so in practice this cap
# only catches non-video and files ffmpeg could not shrink.
MAX_MB = int(os.environ.get("VAULT_UPLOAD_MAX_MB") or 200)
MAX_BYTES = MAX_MB * 1024 * 1024
# Free space we refuse to drop below while spooling. A batch that fills the
# disk breaks the whole relay, not just itself.
MIN_FREE_BYTES = int(os.environ.get("VAULT_UPLOAD_MIN_FREE_MB") or 2048) * 1024 * 1024
# How large a source we are willing to FETCH, as opposed to upload. These differ
# because compression sits between them: a 450 MB Drive video is far over the
# upload ceiling but becomes ~85 MB once re-encoded, and we cannot compress what
# we refused to download. Only compressible video is allowed past MAX_MB here;
# anything else is still capped at what OnlyFans will actually take.
MAX_DOWNLOAD_MB = int(os.environ.get("VAULT_DOWNLOAD_MAX_MB") or 2000)
MAX_DOWNLOAD_BYTES = MAX_DOWNLOAD_MB * 1024 * 1024
# Pause between files. MEASURED, not guessed: OnlyFans throttles POST /posts to
# roughly one per ten seconds and 400s with "Please allow 10 seconds" when a
# batch of small files outruns it. Each file needs one carrier post, so this is
# the real floor on batch throughput — 40 files is ~7 minutes minimum. The
# carrier call retries on its own too (of_client._CARRIER_MIN_GAP_S); pacing
# here just avoids provoking it in the first place.
PACE_S = float(os.environ.get("VAULT_UPLOAD_PACE_S") or 11.0)

# One run per account, in-process. Deliberately NOT read from run.json: a stale
# "running" file from a killed process must never lock the feature out forever.
_running: set[str] = set()
_lock = threading.Lock()


# Drive-content-md5 -> vault id, remembered across runs. Drive publishes an md5
# for every binary file in the LISTING, for free — so a re-import can recognise
# content it already uploaded and never spend the download.
#
# This is not a nicety. The vault's own dedupe (an S3 ETag) can only be computed
# from bytes we have already fetched, so without this memo every re-run
# re-downloads the whole folder to discover it has nothing to do. Doing that
# twice in ten minutes got this key served Google's "Sorry…" throttle page.
_MEMO_PATH = SPOOL_ROOT / "drive_memo.json"


def _memo_load() -> dict:
    import json
    try:
        return json.loads(_MEMO_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _memo_remember(account_id: str, drive_md5: str, vault_id) -> None:
    if not drive_md5 or not vault_id:
        return
    memo = _memo_load()
    memo.setdefault(str(account_id), {})[drive_md5] = vault_id
    SPOOL_ROOT.mkdir(parents=True, exist_ok=True)
    atomic_json.write_atomic(_MEMO_PATH, memo)


def _memo_lookup(account_id: str, drive_md5: str, client) -> int | None:
    """A remembered vault id, confirmed to still exist.

    Verified rather than trusted: vault items can be hidden or removed between
    runs, and handing back a dead id would silently drop a file the operator
    asked for.
    """
    if not drive_md5:
        return None
    vid = _memo_load().get(str(account_id), {}).get(drive_md5)
    if not vid:
        return None
    try:
        client.vault_media_by_id(int(vid))
        return int(vid)
    except Exception:  # noqa: BLE001 — a stale memo entry is not an error
        log.info("vault memo: %s no longer resolves, will re-upload", vid)
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_dir(run_id: str) -> Path:
    return SPOOL_ROOT / run_id


def _state_path(run_id: str) -> Path:
    return _run_dir(run_id) / "run.json"


def read_state(run_id: str) -> dict | None:
    import json
    try:
        return json.loads(_state_path(run_id).read_text())
    except (OSError, ValueError):
        return None


def _save(state: dict) -> None:
    atomic_json.write_atomic(_state_path(state["run_id"]), state)


def is_running(account_id: str) -> bool:
    return str(account_id) in _running


def latest_run(account_id: str) -> dict | None:
    """Most recent run for an account, for a status poll after a page reload."""
    runs = []
    for d in _dirs():
        st = read_state(d.name)
        if st and str(st.get("account_id")) == str(account_id):
            runs.append(st)
    if not runs:
        return None
    latest = max(runs, key=lambda s: s.get("created_at") or "")
    return _reconcile(latest)


def _dirs() -> list[Path]:
    if not SPOOL_ROOT.is_dir():
        return []
    return [d for d in SPOOL_ROOT.iterdir() if d.is_dir()]


def _reconcile(state: dict) -> dict:
    """A run whose file says 'running' but whose process is gone is finished —
    it just never got to say so. Report that honestly rather than leaving a
    status endpoint claiming progress forever."""
    if state.get("status") == "running" and str(state.get("account_id")) not in _running:
        return {**state, "status": "interrupted",
                "error": state.get("error") or "relay restarted mid-run"}
    return state


# ── the run ───────────────────────────────────────────────────────────────

def start(account_id: str, *, client, paced_post=None,
          local_paths: list[str] | None = None,
          drive_links: list[str] | None = None,
          list_id: int | None = None,
          max_files: int = MAX_FILES,
          max_bytes: int = MAX_BYTES) -> dict:
    """Run a whole batch, synchronously. Returns the final state.

    BLOCKING and network-bound — the caller must hand this to a thread, never
    run it on the relay's event loop. `client` is an OFClient for `account_id`.

    `paced_post(**kwargs)` is an optional carrier-post callable that routes
    through the relay's per-account write pacer. Pass it from inside the relay,
    where automations are also writing to the same account; omit it in a
    standalone script, where nothing else is competing for the write window.
    """
    aid = str(account_id)
    with _lock:
        if aid in _running:
            raise RuntimeError("a vault upload is already running for this account")
        _running.add(aid)

    run_id = f"{aid}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    rd = _run_dir(run_id)
    rd.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {
        "run_id": run_id, "account_id": aid, "status": "running",
        "phase": "collecting", "created_at": _now(), "finished_at": None,
        "list_id": list_id, "error": None, "items": [],
    }
    _save(state)

    try:
        items = _collect(state, local_paths or [], drive_links or [],
                         client=client, max_files=max_files, max_bytes=max_bytes)
        state["items"] = items
        state["phase"] = "uploading"
        _save(state)

        for item in items:
            if item["status"] != "pending":
                continue
            _upload_one(state, item, client, paced_post)
            _save(state)
            time.sleep(PACE_S)

        _file_into_folder(state, client)
        state["status"] = "done"
    except Exception as e:
        log.exception("vault batch %s failed", run_id)
        state["status"] = "failed"
        state["error"] = f"{type(e).__name__}: {e}"
    finally:
        state["phase"] = "finished"
        state["finished_at"] = _now()
        _save(state)
        _cleanup_carriers(state, client)
        _clear_spool(state)
        _save(state)
        with _lock:
            _running.discard(aid)
    return state


def _collect(state: dict, local_paths: list[str], drive_links: list[str],
             *, client, max_files: int, max_bytes: int) -> list[dict]:
    """Build the work list. Drive files are downloaded into the run's spool;
    local files are used where they are and never deleted (they're the
    operator's own originals — only spooled copies get cleared)."""
    items: list[dict] = []

    for p in local_paths:
        path = Path(p)
        size = path.stat().st_size if path.exists() else None
        items.append(_item(path.name, str(path), "local", size, spooled=False))

    if drive_links:
        import gdrive
        files = gdrive.resolve(drive_links, max_files=max_files * 2)
        _guard_space(sum(f.size or 0 for f in files))
        for f in files:
            ok, why = gdrive.is_uploadable(f)
            if not ok:
                items.append(_item(f.name, None, "gdrive", f.size,
                                   status="skipped", error=why))
                continue
            if len(([i for i in items if i["status"] == "pending"])) >= max_files:
                items.append(_item(f.name, None, "gdrive", f.size,
                                   status="skipped",
                                   error=f"batch cap of {max_files} files reached"))
                continue
            # Already imported? Recognise it from Drive's own md5, before
            # spending the download. Without this a re-run re-fetches the whole
            # folder just to discover every file is a duplicate.
            if known := _memo_lookup(state["account_id"], f.md5, client):
                it = _item(f.name, None, "gdrive", f.size,
                           status="done", error=None)
                it.update(vault_id=known, deduped=True, drive_md5=f.md5)
                items.append(it)
                state["items"] = items
                _save(state)
                continue
            # Video that ffmpeg can shrink is fetched up to the (much larger)
            # download ceiling; _upload_one compresses it and re-checks the real
            # upload cap afterwards. Everything else stays capped at max_bytes,
            # since nothing downstream can make it smaller.
            compressible = (media_prep.have_ffmpeg()
                            and f.mime_type.startswith("video/"))
            fetch_cap = MAX_DOWNLOAD_BYTES if compressible else max_bytes
            if f.size is not None and f.size > fetch_cap:
                items.append(_item(f.name, None, "gdrive", f.size, status="skipped",
                                   error=f"{f.size / 1e6:.0f} MB exceeds the "
                                         f"{fetch_cap / 1e6:.0f} MB download limit"))
                state["items"] = items
                _save(state)
                continue
            try:
                dest = gdrive.download(f, _run_dir(state["run_id"]), max_bytes=fetch_cap)
                it = _item(f.name, str(dest), "gdrive", f.size, spooled=True)
                it["drive_md5"] = f.md5
                items.append(it)
            except Exception as e:
                items.append(_item(f.name, None, "gdrive", f.size,
                                   status="failed", error=str(e)))
            state["items"] = items
            _save(state)

    # Enforce the caps on whatever is still pending, oversize first so the
    # operator sees *why* a file was dropped rather than just a short list.
    pending = 0
    for it in items:
        if it["status"] != "pending":
            continue
        if it["size"] is not None and it["size"] > max_bytes:
            # Oversized VIDEO is not rejected here — compression runs later and
            # is the whole reason a 443 MB file can be imported at all. Checking
            # the cap against the source size would reject exactly the files the
            # compression step exists to rescue. The post-compression size is
            # re-checked in _upload_one, where the real number is known.
            if it.get("path") and media_prep.have_ffmpeg() \
                    and media_prep.is_video(Path(it["path"])):
                continue
            it.update(status="skipped",
                      error=f"{it['size'] / 1e6:.0f} MB exceeds the "
                            f"{max_bytes / 1e6:.0f} MB per-file limit "
                            f"(and cannot be compressed)")
            continue
        pending += 1
        if pending > max_files:
            it.update(status="skipped", error=f"batch cap of {max_files} files reached")
    return items


def _item(name, path, source, size, *, status="pending", error=None,
          spooled=False) -> dict:
    return {"name": name, "path": path, "source": source, "size": size,
            "status": status, "error": error, "spooled": spooled,
            "vault_id": None, "carrier_post_id": None, "carrier_deleted": None,
            "deduped": None}


def _guard_space(incoming: int) -> None:
    SPOOL_ROOT.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(SPOOL_ROOT).free
    if free - incoming < MIN_FREE_BYTES:
        raise RuntimeError(
            f"not enough disk: {free / 1e9:.1f} GB free, batch needs "
            f"~{incoming / 1e9:.1f} GB and {MIN_FREE_BYTES / 1e9:.1f} GB must stay free")


def _upload_one(state: dict, item: dict, client, paced_post=None) -> None:
    """One file: upload, mint a vault id, drop the spooled copy."""
    path = item.get("path")
    if not path or not Path(path).exists():
        item.update(status="failed", error="file missing from spool")
        return

    def remember_carrier(post_id) -> None:
        # Before the poll, before anything else — this is the record that makes
        # a crash recoverable instead of a scheduled post nobody knows about.
        item["carrier_post_id"] = post_id
        item["carrier_deleted"] = False
        _save(state)

    # Shrink oversized video first. OF's convert step answers 504 on large
    # objects, so this is not an optimisation — above the ceiling it is the
    # difference between an upload that lands and one that cannot.
    upload_path, compressed = media_prep.prepare(path, _run_dir(state["run_id"]))
    if compressed:
        item["compressed_from"] = item["size"]
        item["size"] = Path(upload_path).stat().st_size
        _save(state)

    # Now that the real upload size is known, enforce the ceiling. Above it,
    # OnlyFans' convert step answers a hard 504 AFTER every byte has been
    # stored, so refusing here saves a pointless multi-minute upload.
    final_size = Path(upload_path).stat().st_size
    if final_size > MAX_BYTES:
        item.update(status="skipped",
                    error=(f"{final_size / 1e6:.0f} MB is over the "
                           f"{MAX_BYTES / 1e6:.0f} MB limit"
                           + (" even after compression" if compressed else "")
                           + " — OnlyFans rejects objects this large"))
        if compressed and str(upload_path) != str(path):
            Path(upload_path).unlink(missing_ok=True)
        return

    item["status"] = "uploading"
    _save(state)
    try:
        res = client.upload_to_vault(str(upload_path), on_carrier=remember_carrier,
                                     create_post_fn=paced_post)
        # Bytes reached S3 but the claim failed (typically a convert 504 on a
        # big object). The expensive half is done and the S3 object persists,
        # so retry just the claim rather than re-uploading everything.
        up = res.get("upload") or {}
        if not res.get("vault_id") and up.get("upload_key") and not up.get("ready"):
            for attempt, delay in enumerate((20, 60), start=1):
                log.warning("vault batch: claim failed for %s, retrying in %ss (%d/2)",
                            item["name"], delay, attempt)
                time.sleep(delay)
                again = client.claim_uploaded(
                    upload_key=up["upload_key"], etag=up.get("etag") or "",
                    filename=up.get("filename") or item["name"],
                    size=up.get("size") or 0)
                if again.get("ready"):
                    res = client.materialize_to_vault(
                        again["send_with"], md5_hex=again.get("dedupe_key"),
                        size=again.get("size"), on_carrier=remember_carrier,
                        create_post_fn=paced_post)
                    item["claim_retries"] = attempt
                    break
        item["vault_id"] = res.get("vault_id")
        item["deduped"] = res.get("deduped")
        if res.get("carrier_post_id"):
            item["carrier_post_id"] = res["carrier_post_id"]
            item["carrier_deleted"] = bool(res.get("carrier_deleted"))
        item["status"] = "done" if res.get("vault_id") else "failed"
        if not res.get("vault_id"):
            item["error"] = res.get("note") or "no vault id"
    except Exception as e:
        log.exception("vault upload failed for %s", item["name"])
        item.update(status="failed", error=f"{type(e).__name__}: {e}")

    # Clear the spooled copy as soon as it is no longer needed — the whole
    # point of "push it up and let go of it" is that the disk stays flat.
    # The compressed copy is always ours to delete; the source only if we
    # spooled it (a local file is the operator's own original).
    if compressed and str(upload_path) != str(path):
        Path(upload_path).unlink(missing_ok=True)
    if item["status"] == "done" and item.get("drive_md5") and item.get("vault_id"):
        _memo_remember(state["account_id"], item["drive_md5"], item["vault_id"])
    if item["status"] == "done" and item.get("spooled"):
        Path(path).unlink(missing_ok=True)
        item["path"] = None


def _file_into_folder(state: dict, client) -> None:
    """Put everything that succeeded into the target vault list, in one call."""
    list_id = state.get("list_id")
    ids = [i["vault_id"] for i in state["items"] if i.get("vault_id")]
    if not list_id or not ids:
        return
    try:
        client.add_media_to_vault_list(int(list_id), ids)
        state["filed_into_list"] = len(ids)
    except Exception as e:
        # Never fatal, and never a reason to re-upload: the media is already in
        # the vault, just not in the folder the operator picked.
        log.warning("vault batch %s: foldering failed — %s", state["run_id"], e)
        state["filed_into_list"] = 0
        state["error"] = (state.get("error") or "") + f" (foldering failed: {e})"


def _cleanup_carriers(state: dict, client) -> None:
    """Delete any carrier still recorded as live. Normally a no-op — the
    per-file path deletes its own — but this is what catches the ones a
    mid-run exception skipped."""
    for it in state["items"]:
        pid = it.get("carrier_post_id")
        if not pid or it.get("carrier_deleted"):
            continue
        try:
            client.delete_post(pid)
            it["carrier_deleted"] = True
        except Exception as e:
            log.error("VAULT CARRIER LEFT BEHIND: post %s (%s) — %s",
                      pid, it.get("name"), e)


def _clear_spool(state: dict) -> None:
    """Remove the run directory's media, keeping run.json for the status poll.

    Failed items' files go too: a retry re-downloads or re-picks them, and
    keeping them is how a VPS quietly fills up.
    """
    rd = _run_dir(state["run_id"])
    if not rd.is_dir():
        return
    freed = 0
    for f in rd.iterdir():
        if f.name == "run.json":
            continue
        try:
            freed += f.stat().st_size
            f.unlink()
        except OSError as e:
            log.warning("vault batch: could not clear %s — %s", f, e)
    for it in state["items"]:
        if it.get("spooled"):
            it["path"] = None
    state["spool_cleared_bytes"] = freed


# ── crash recovery ────────────────────────────────────────────────────────

def sweep(client_for: Callable[[str], Any], *, keep_hours: int = 48) -> dict:
    """Boot-time backstop: delete orphaned carriers, bin stale run dirs.

    `client_for(account_id)` supplies an OFClient. This is the piece the
    reference implementation has no equivalent of, and the reason a crash here
    costs nothing worse than a re-run.
    """
    fixed, failed, removed = 0, 0, 0
    cutoff = time.time() - keep_hours * 3600
    for d in _dirs():
        state = read_state(d.name)
        if state is None:
            # No state file — nothing can be recovered from it, and its media
            # is unreferenced. Bin it if it's old enough to be certainly dead.
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
            continue

        live = [i for i in state.get("items", [])
                if i.get("carrier_post_id") and not i.get("carrier_deleted")]
        if live:
            try:
                client = client_for(str(state["account_id"]))
                for it in live:
                    try:
                        client.delete_post(it["carrier_post_id"])
                        it["carrier_deleted"] = True
                        fixed += 1
                        log.warning("vault sweep: deleted orphaned carrier %s",
                                    it["carrier_post_id"])
                    except Exception as e:
                        failed += 1
                        log.error("vault sweep: carrier %s STILL LIVE — %s",
                                  it["carrier_post_id"], e)
            except Exception as e:
                failed += len(live)
                log.error("vault sweep: no client for account %s — %s",
                          state.get("account_id"), e)
        if state.get("status") == "running":
            state["status"] = "interrupted"
            state["error"] = "relay restarted mid-run"
        _save(state)

        if state.get("finished_at") and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1

    if fixed or failed or removed:
        log.info("vault sweep: %d carriers deleted, %d still live, %d dirs removed",
                 fixed, failed, removed)
    return {"carriers_deleted": fixed, "carriers_failed": failed,
            "dirs_removed": removed}
