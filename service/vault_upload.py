"""Batch-upload media into the OnlyFans vault, from disk or Google Drive.

THE SHAPE OF THE PROBLEM. OnlyFans has no "save to vault" endpoint (see
of_client.materialize_to_vault for the live evidence). Media enters the vault
only when something references it, so every fresh file costs a carrier post
that we create, read a vault id off, and delete. That makes a bulk import a
long sequence of small, individually-failable, individually-DANGEROUS steps:
a carrier that outlives its run is a scheduled post that eventually publishes
to the creator's whole feed.

So this module is built around two rules the reference implementation breaks:

  1. **The INTENT to create a carrier is written to disk before the carrier
     exists**, and the record survives the process. The id itself cannot be
     written first — OnlyFans mints it — so the pre-POST record carries the
     marker instead, and `sweep()` reconciles it against OnlyFans' own
     scheduled-post list. That is what covers the worst case: the POST landed,
     its response did not, and no id was ever learned. `of_client` holds the id
     on the stack and deletes it in a `finally`, which a SIGKILL does not run.
  2. **The spool is the run's own responsibility.** Files land in
     `service/vault_spool/<run_id>/`, are deleted as each one succeeds, and the
     directory goes when the run ends — including when it ends badly. A VPS
     that fills its disk with abandoned gigabytes takes SQLite down with it.
  3. **"Is this live?" is asked in exactly one place.** Every carrier decision
     turns on it — the sweep deleting one, the reconciler calling a run
     interrupted, the warning telling an operator one escaped — and for two
     rounds it had three implementations that disagreed, so one HTTP response
     could call a run interrupted while suppressing its warnings because the
     account was busy. `_run_is_live` is the predicate; `_account_is_live` is
     the same question at account width, which only the sweep asks. There is no
     third way to ask it, and adding one is how the deleted-a-live-carrier bug
     came back twice.

FILES are processed one at a time on purpose, and that is not a bandwidth
decision — OnlyFans throttles post creation to about one per ten seconds, and
every file needs one carrier post, so parallel files would only queue up against
that limit while multiplying the number of live carriers at any instant. (The
PARTS of a single file do upload concurrently; see of_client._PART_CONCURRENCY,
measured at 2x on a home connection — it lives there because it is a property of
the S3 transport, not of a batch.)

State lives in `<spool>/<run_id>/run.json` rather than a DB table: it must be
readable by a sweep that runs before anything else boots, it is per-run garbage
that should die with its directory, and `atomic_json` already gives us a
never-half-written small file.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import atomic_json
import gdrive
import media_prep
import of_client

log = logging.getLogger("of-relay.vault_upload")

HERE = Path(__file__).resolve().parent
SPOOL_ROOT = Path(os.environ.get("VAULT_SPOOL_DIR") or (HERE / "vault_spool"))
LOCK_DIR = SPOOL_ROOT / "locks"
# Where the HTTP route stages an uploaded batch before the run adopts it. Under
# the spool rather than TMPDIR so that a SIGKILL between staging and the run
# leaves the bytes somewhere `sweep()` knows about — a container never cleans
# /tmp, and this feature's whole point is that it does not fill the disk.
STAGE_ROOT = SPOOL_ROOT / "incoming"

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
# batch of small files outruns it. This is the STANDALONE backstop only: the
# relay's own write gate already spaces every write for the account, so the
# route passes `Limits(pace_s=0)` and sleeping here on top of it would double
# the cost of every file. That is a stated policy, not something inferred from
# whether a gate was injected (see start()).
PACE_S = float(os.environ.get("VAULT_UPLOAD_PACE_S") or 11.0)

# The vocabulary. Declared once so the writer, the reconciler, the HTTP status
# route, the CLI's mark map and the dashboard are all naming the same states —
# they had already drifted by one value ("interrupted", which no writer
# produced) when this was five separate lists.
ITEM_STATUSES = ("pending", "uploading", "done", "failed", "skipped")
RUN_STATUSES = ("running", "done", "failed", "interrupted")

_RUN_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}-\d{8}T\d{6}-[0-9a-f]{6}")


@dataclass(frozen=True)
class Limits:
    """Every size/count policy for one run, in one object.

    These used to be four module constants read from four places, with one of
    them (`MAX_BYTES`) read as a global by the very function a `--max-mb`
    override was supposed to reach — so `--max-mb 50` capped collection at 50 MB
    and then admitted a 200 MB compressed file. One object, built once in
    `start()` and threaded through, is what makes an override actually mean
    something end to end.

    `target_bytes < max_bytes` is the cross-module invariant: `media_prep` aims
    a re-encode at the target, and a target above the upload ceiling means every
    large video pays a full ffmpeg pass and is then skipped. It was asserted in
    a comment and enforced nowhere; it is enforced here.
    """
    max_files: int = MAX_FILES
    max_bytes: int = MAX_BYTES
    max_download_bytes: int = MAX_DOWNLOAD_BYTES
    compress_over_bytes: int = media_prep.COMPRESS_OVER_MB * 1024 * 1024
    target_bytes: int = media_prep.TARGET_MB * 1024 * 1024
    min_free_bytes: int = MIN_FREE_BYTES
    pace_s: float = PACE_S

    def checked(self) -> "Limits":
        """The same limits with the invariant repaired, loudly."""
        if self.target_bytes < self.max_bytes:
            return self
        target = int(self.max_bytes * 0.95)
        log.error("vault limits: compression target %.0f MB is not below the "
                  "%.0f MB upload ceiling — every large video would be "
                  "re-encoded and then skipped. Clamping the target to %.0f MB; "
                  "fix VAULT_COMPRESS_TARGET_MB / VAULT_UPLOAD_MAX_MB.",
                  self.target_bytes / 1e6, self.max_bytes / 1e6, target / 1e6)
        return replace(self, target_bytes=target)

    @property
    def compress_over_mb(self) -> int:
        return max(1, self.compress_over_bytes // (1024 * 1024))

    @property
    def target_mb(self) -> int:
        return max(1, self.target_bytes // (1024 * 1024))


# One run per account, in-process: account id -> the run id it is running.
# Deliberately NOT read from run.json: a stale "running" file from a killed
# process must never lock the feature out forever. Keyed by run id as well as
# account so a finished run cannot read as live just because a LATER run is.
# Cross-process exclusion is a separate mechanism — see _acquire_lock.
_live: dict[str, str] = {}
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
# The file is shared by every account. Each write is atomic; a read-modify-write
# is not, so two imports running at once would drop each other's entries. This
# lock closes that within the process. ACROSS processes it is still a race — but
# only between DIFFERENT accounts, since one account cannot have two importers
# (see _acquire_lock), and the cost of losing an entry is one re-download that
# the next run re-remembers, never a wrong answer.
_memo_lock = threading.Lock()


def _memo_load() -> dict:
    try:
        return json.loads(_MEMO_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _memo_remember(account_id: str, drive_md5: str, vault_id) -> None:
    if not drive_md5 or not vault_id:
        return
    with _memo_lock:
        memo = _memo_load()
        memo.setdefault(str(account_id), {})[drive_md5] = vault_id
        SPOOL_ROOT.mkdir(parents=True, exist_ok=True)
        atomic_json.write_atomic(_MEMO_PATH, memo)


def _memo_lookup(account_id: str, drive_md5: str, client) -> int | None:
    """A remembered vault id, confirmed to still exist AND still be visible.

    Verified rather than trusted: vault items can be hidden or removed between
    runs, and handing back a dead id would silently drop a file the operator
    asked for. A HIDDEN item is treated as absent on purpose — the id resolves,
    but the media is not in the vault the operator is looking at, and reporting
    that as "already in vault" is how a file is lost without anyone noticing.
    """
    if not drive_md5:
        return None
    with _memo_lock:
        vid = _memo_load().get(str(account_id), {}).get(drive_md5)
    if not vid:
        return None
    try:
        media = client.vault_media_by_id(int(vid)) or {}
    except Exception:  # noqa: BLE001 — a stale memo entry is not an error
        log.info("vault memo: %s no longer resolves, will re-upload", vid)
        return None
    if media.get("hidden") or media.get("isHidden"):
        log.info("vault memo: %s is hidden in the vault, treating as absent", vid)
        return None
    return int(vid)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_dir(run_id: str) -> Path:
    if not _RUN_ID_RE.fullmatch(run_id or ""):
        # `run_id` arrives from a query string. The account check downstream
        # makes it only an existence probe, but a value that is joined onto a
        # filesystem path gets validated whatever else guards it.
        raise ValueError(f"not a run id: {run_id!r}")
    return SPOOL_ROOT / run_id


def _state_path(run_id: str) -> Path:
    return _run_dir(run_id) / "run.json"


def read_state(run_id: str, cache: dict | None = None) -> dict | None:
    """A run's state, always reconciled.

    Reconciliation lives here rather than in `latest_run` because a reader that
    can forget it will: polling a named run whose relay had restarted reported
    `status: "running"` forever, the one lie this module exists to avoid.

    `cache` is the per-request liveness cache — one process lookup for a whole
    walk of the spool instead of one per run dir.
    """
    try:
        state = json.loads(_state_path(run_id).read_text())
    except (OSError, ValueError):
        return None
    return _reconcile(state, cache)


def _save(state: dict) -> None:
    atomic_json.write_atomic(_state_path(state["run_id"]), state)


def _intent_unresolved(item: dict) -> bool:
    """True while an intent might name a carrier nothing has accounted for.

    An intent is written before EVERY carrier POST, and is never cleared once
    an id is learned -- so `carrier_intent` alone does not mean "outstanding".
    Only an intent with no id is unaccounted for, and only until a completed
    marker scan says OnlyFans has no such post (`carrier_scan_clear`).

    This is the one definition. Pass 1's scan trigger used to spell it without
    the id check, so every successfully imported file kept its account in the
    hourly scan for the 48 h its run dir was retained -- a full paged walk of
    the creator's schedule, hourly, for nothing.
    """
    if item.get("carrier_write_pending"):
        return True
    return bool(item.get("carrier_intent")
                and not item.get("carrier_post_id")
                and not item.get("carrier_scan_clear"))


def account_runs(account_id: str) -> list[dict]:
    """Every run record for one account, reconciled, read ONCE.

    The status route needs the latest run AND the account-wide carrier roll-up,
    and computing them independently walked the spool and re-parsed every
    `run.json` twice for every 2.5 s poll of every open tab.
    """
    cache: dict[str, bool] = {}
    return [st for d in _dirs()
            if (st := read_state(d.name, cache))
            and str(st.get("account_id")) == str(account_id)]


def latest_of(runs: list[dict]) -> dict | None:
    """Most recent run, for a status poll after a page reload."""
    return max(runs, key=lambda s: s.get("created_at") or "") if runs else None


def latest_run(account_id: str) -> dict | None:
    return latest_of(account_runs(account_id))


def carriers_outstanding(account_id: str, runs: list[dict]) -> tuple[int, int]:
    """`(undeletable carriers, carriers that may exist unrecorded)`.

    Across EVERY run for the account, not just the last: a carrier that could
    not be deleted is a scheduled post on a real feed, and if the warning
    vanished the moment a newer import replaced the "latest" run the operator
    would stop being told about it a day before it published.

    Two rules the first version got wrong in opposite directions:

      * only FINISHED runs count. A carrier legitimately exists for the couple
        of seconds between its create and its delete, so counting live runs made
        a real alarm fire on the happy path of every normal import — and an
        alarm that cries wolf is one the operator stops reading.
      * an INTENT counts too, on its own. The dangerous carrier is precisely the
        one with no id: the create response was lost, so nothing here can name
        it and only the marker sweep can find it. Counting ids alone reported
        "all clear" in the single case where it might not be.

    "Finished" is `_run_is_live`'s answer and not a second opinion. This used to
    re-implement it inline as "the account is busy, or this run says running",
    which silenced a WEEK-OLD run's real alarm for as long as any other import
    was going — and one wedged run kept `aid` in `_live` for the life of the
    relay, so "as long as any other import was going" could mean forever.
    """
    undeletable = unrecorded = 0
    # One liveness answer for the whole account: the lock check can cost a
    # process lookup, and this runs on a 2.5 s poll.
    cache: dict[str, bool] = {}
    for st in runs:
        if _run_is_live(st, cache):
            continue
        for i in st.get("items") or []:
            if i.get("carrier_post_id"):
                if not i.get("carrier_deleted"):
                    undeletable += 1
            elif _intent_unresolved(i):
                unrecorded += 1
    return undeletable, unrecorded


def _dirs() -> list[Path]:
    if not SPOOL_ROOT.is_dir():
        return []
    return [d for d in SPOOL_ROOT.iterdir()
            if d.is_dir() and _RUN_ID_RE.fullmatch(d.name)]


def _reconcile(state: dict, cache: dict | None = None) -> dict:
    """A run whose file says 'running' but whose process is gone is finished —
    it just never got to say so. Report that honestly rather than leaving a
    status endpoint claiming progress forever.

    The question is `_run_is_live`'s and nobody else's. Asking a narrower one
    here — the in-process registry alone — meant a CLI import holding the
    cross-process lock was reported as "the relay restarted mid-run" in the same
    payload that suppressed its carrier warnings because the account was busy.
    """
    if state.get("status") != "running" or _run_is_live(state, cache):
        return state
    return {**state, "status": "interrupted",
            "error": state.get("error") or "relay restarted mid-run"}


# ── cross-process exclusion ───────────────────────────────────────────────

# A lock nobody can be holding any more. An import of forty large videos is
# hours, not days, so anything older than this is a crash whose pid has since
# been recycled — the case that used to lock the feature out permanently.
_LOCK_MAX_AGE_S = 24 * 3600
# Same ceiling for the in-process registry. `_live` has no expiry of its own,
# and a run CAN wedge — a Drive server trickling bytes under a per-read socket
# timeout, a write gate waiting on a pacer that never comes round. While `aid`
# sits in `_live` the sweep leaves the whole account alone and the operator is
# told its carriers are clean, so an unbounded `_live` entry defers invariant 1
# for the life of the relay process, which here is weeks. `_save` runs after
# every item, so run.json's mtime is a live run's heartbeat.
_LIVE_MAX_STALL_S = 6 * 3600
# How long a pid's start stamp is trusted without re-reading it.
_PID_START_TTL_S = 30.0
_pid_start_memo: dict[int, tuple[float, str | None]] = {}

# THIS process, for as long as it lives. A pid is only meaningful inside one pid
# namespace: a second container mounting the same spool sees the holder's pid
# number attached to one of its OWN processes, reads a different start stamp,
# and would call a genuinely live lock stale. The instance id says "that record
# was not written by anyone I can see", which is UNKNOWN, not dead.
_INSTANCE_ID = uuid.uuid4().hex


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True      # EPERM — someone else's process, but alive
    return True


def _pid_start(pid: int) -> str | None:
    """An opaque, comparable start-time stamp for `pid`, or None if unknowable.

    Pid ALONE cannot answer "is my lock holder still alive": container pids are
    small and recycled within minutes, so a relay OOM-killed at pid 37 whose pid
    is taken by any new thread-spawning process reads as live forever. The start
    time makes the identity unforgeable — a recycled pid has a different one.

    Memoised for `_PID_START_TTL_S`, because on a non-Linux host this forks
    `ps` and the status route asks it on a 2.5 s poll. The TTL is what bounds
    the staleness: a pid recycled inside the window reads as its predecessor
    for at most that long, which delays a stale-lock decision and never makes
    a permanent one.
    """
    now = time.time()
    hit = _pid_start_memo.get(pid)
    if hit and now - hit[0] < _PID_START_TTL_S:
        return hit[1]
    stamp = None
    try:
        # Linux: field 22 of /proc/<pid>/stat, after the (possibly space- and
        # bracket-bearing) comm field.
        raw = Path(f"/proc/{pid}/stat").read_text()
        stamp = raw.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError, ValueError):
        try:
            out = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                                 capture_output=True, text=True, timeout=5)
            stamp = out.stdout.strip() or None
        except Exception:  # noqa: BLE001 — an unknown start time is not an error
            stamp = None
    _pid_start_memo[pid] = (now, stamp)
    return stamp


def _lock_path(account_id: str) -> Path:
    return LOCK_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', str(account_id))}.lock"


def _read_lock(path: Path) -> dict:
    try:
        rec = json.loads(path.read_text())
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def _lock_holder(account_id: str) -> dict | None:
    """The record of a LIVE importer holding this account's lock, or None.

    Stale — and only these — means: unreadable, its pid gone, its pid alive but
    STARTED AT A DIFFERENT TIME *while the record came from a process we can
    actually see* (a recycled pid, the permanent-lockout case), or simply older
    than any real import could be.

    A record written by ANOTHER instance whose start stamp does not match is
    UNKNOWN, not stale: across pid namespaces the same number names a different
    process, so "the start times differ" stops being evidence of death. We
    refuse rather than steal — a refused import is a message on screen; a stolen
    lock is two importers on one account, which is what the lock exists to stop.
    The age ceiling below is what keeps that from being permanent.
    """
    p = _lock_path(account_id)
    rec = _read_lock(p)
    pid = rec.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    if not _pid_alive(pid):
        return None
    started, instance = rec.get("started"), rec.get("instance")
    now_started = _pid_start(pid)
    if (started and now_started and started != now_started
            and instance in (None, _INSTANCE_ID)):
        return None      # same pid number, same namespace, different process
    try:
        if time.time() - p.stat().st_mtime > _LOCK_MAX_AGE_S:
            return None
    except OSError:
        return None
    return rec


def _stalled(run_id: str | None) -> bool:
    """Has this run stopped writing for longer than any real one goes quiet?

    The `_live` half of liveness has no expiry of its own; this is it. Unknowable
    (no run id, no state file yet) counts as NOT stalled — a run that has only
    just been claimed must never read as dead.
    """
    try:
        return time.time() - _state_path(run_id).stat().st_mtime > _LIVE_MAX_STALL_S
    except (OSError, ValueError, TypeError):
        return False


def _account_is_live(account_id: str, cache: dict | None = None) -> bool:
    """Is ANY importer working on this account, in this process or another?

    THE liveness atom, and `_run_is_live` below is the same question narrowed to
    one run. `_live` alone cannot answer it — that is why the lockfile exists —
    and the lockfile alone cannot either, since the CLI's sweep runs in a
    process that holds neither. Both signals, and a carrier is only ever touched
    when both say no. `cache` spares a caller looping over many run dirs one
    process lookup per dir.

    The SWEEP is what asks at this width, deliberately: it leaves a busy account
    alone whole rather than picking which of its carriers are orphaned, because
    that guess is the one that deletes a live carrier. Everything a person reads
    — the run's status, the carrier warnings — asks the narrower question, so a
    week-old escape is not hidden by today's import.
    """
    aid = str(account_id)
    if cache is not None and aid in cache:
        return cache[aid]
    mine = _live.get(aid)
    answer = (mine is not None and not _stalled(mine)) or _lock_holder(aid) is not None
    if cache is not None:
        cache[aid] = answer
    return answer


def _run_is_live(state: dict, cache: dict | None = None) -> bool:
    """Is THIS RUN still being worked on? The only place that decides.

    Everything that needs an answer asks here: the reconciler that decides
    whether a `running` record is telling the truth, the operator warnings, and
    (through `_account_is_live`) the sweep. There used to be three answers — a
    named predicate, a character-for-character copy inside the warnings, and a
    narrower `_live`-only rule in the reconciler — and one HTTP response could
    call a run "interrupted" while suppressing its warnings because the account
    was busy. Two answers to one question in one dict.

    The rules, in order:

      * `finished_at` is written exactly once, in `start()`'s `finally`. A run
        that has it is over, in this process or any other. It is the only
        cross-process-reliable "not live" there is.
      * this process knows exactly which run it is running, keyed by RUN id so
        an older run of a busy account does not read as live.
      * otherwise the lockfile is the only witness, and it names its run — so a
        CLI import in another process reads as live for its own run and for
        nothing else.
    """
    if state.get("finished_at") or state.get("status") != "running":
        return False
    aid, run_id = str(state.get("account_id")), state.get("run_id")
    mine = _live.get(aid)
    if mine is not None:
        return mine == run_id and not _stalled(run_id)
    holder = _lock_holder(aid) if _account_is_live(aid, cache) else None
    # A lock written before run ids were recorded cannot name its run; treat it
    # as possibly this one rather than deleting somebody's live carrier.
    return bool(holder) and holder.get("run_id") in (None, run_id)


def _acquire_lock(account_id: str, run_id: str | None = None) -> Path:
    """Exclude a SECOND PROCESS from importing for the same account.

    The in-memory registry above cannot do this, and the CLI's own documented
    invocation runs it inside the relay container — two processes, two pacers,
    two live carriers and a stream of "Please allow 10 seconds" that also hits
    fan automations. An O_EXCL file carrying the pid, its start time, this
    instance's id AND the run it belongs to is stale-checked by process
    identity, never by the on-disk `running` flag, so a killed importer cannot
    lock the feature out — not even when its pid number is handed to something
    else.
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    p = _lock_path(account_id)
    for _ in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, json.dumps({
                    "pid": os.getpid(), "started": _pid_start(os.getpid()),
                    "instance": _INSTANCE_ID, "run_id": run_id,
                    "created_at": _now()}).encode())
            finally:
                os.close(fd)
            return p
        except FileExistsError:
            holder = _lock_holder(account_id)
            if holder and (holder.get("pid") != os.getpid()
                           or holder.get("instance") not in (None, _INSTANCE_ID)):
                raise RuntimeError(
                    f"another process (pid {holder.get('pid')}) is already "
                    f"importing to this account's vault — if that is wrong, "
                    f"delete {p}") from None
            # Our own pid, our own instance — and `begin()` has already checked
            # `_live` under the mutex, so no run of ours is currently using this
            # file. It is the residue of one that already released its claim.
            log.warning("vault: clearing a stale import lock at %s (%s)",
                        p, _read_lock(p) or "unreadable")
            p.unlink(missing_ok=True)
    raise RuntimeError(
        f"could not take the vault import lock for this account ({p})")


def _release_lock(path: Path | None, run_id: str | None = None) -> None:
    """Drop the lock — but only while it still names OUR run.

    `start()` pops its `_live` claim and releases the lock as two statements. A
    second `begin()` landing between them passes the `_live` check, finds our
    own (already finished) record on disk, correctly clears it and takes a fresh
    lock — and the unlink below would then delete the NEW run's file, leaving
    cross-process exclusion silently off for the run that is actually going.
    The run id is what tells the two apart. `abandon()` had the same shape.
    """
    if path is None:
        return
    try:
        if run_id is not None:
            held = _read_lock(path).get("run_id")
            if held is not None and held != run_id:
                log.info("vault: leaving the import lock %s alone — it names "
                         "run %s now, not %s", path, held, run_id)
                return
        path.unlink(missing_ok=True)
    except OSError as e:
        log.warning("vault: could not release the import lock %s — %s", path, e)


# ── the run ───────────────────────────────────────────────────────────────

def _new_state(run_id: str, aid: str, list_id: int | None) -> dict:
    return {
        "run_id": run_id, "account_id": aid, "status": "running",
        "phase": "collecting", "created_at": _now(), "finished_at": None,
        "list_id": list_id, "error": None, "filing_error": None,
        # Declared here even though `_file_into_folder` and `_clear_spool`
        # write them: the CLI and the dashboard both read this record, and a key
        # that only appears once something happened is a key every reader has to
        # guess at (the item schema got this treatment in `_item`).
        "filed_into_list": None, "spool_cleared_bytes": None,
        "items": [],
    }


def begin(account_id: str, *, list_id: int | None = None) -> str:
    """Claim the account and write the run record. Returns the run id.

    SYNCHRONOUS on purpose: the HTTP route calls this before it hands the work
    to a thread, so the response can name the run. Returning before the record
    existed meant the dashboard's first poll raced the worker, was told
    `running: false`, and never armed its interval — the import ran to
    completion with the panel showing the PREVIOUS run's summary.
    """
    aid = str(account_id)
    run_id = f"{aid}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    # In-process claim FIRST. Taking the file lock first meant a second run in
    # this same process found its own pid in the lockfile, treated it as stale,
    # cleared it — and then failed the `_live` check and released the lock the
    # RUNNING import was relying on.
    with _lock:
        if aid in _live:
            raise RuntimeError("a vault upload is already running for this account")
        _live[aid] = run_id
    lock_path = None
    try:
        lock_path = _acquire_lock(aid, run_id)
        # Inside the claim, so a read-only or full spool volume releases it
        # again instead of locking the account out until the relay restarts.
        _run_dir(run_id).mkdir(parents=True, exist_ok=True)
        _save(_new_state(run_id, aid, list_id))
    except Exception:
        with _lock:
            if _live.get(aid) == run_id:
                _live.pop(aid, None)
        _release_lock(lock_path, run_id)
        raise
    return run_id


def abandon(run_id: str, error: str) -> None:
    """Release a run that was claimed by `begin` but never actually ran."""
    state = read_state(run_id)
    aid = str((state or {}).get("account_id") or "")
    if state is not None:
        state.update(status="failed", phase="finished",
                     error=error, finished_at=_now())
        _save(state)
    with _lock:
        if aid and _live.get(aid) == run_id:
            _live.pop(aid, None)
    _release_lock(_lock_path(aid) if aid else None, run_id)


def start(account_id: str, *, client, gate=None,
          local_paths: list[str] | None = None,
          drive_links: list[str] | None = None,
          list_id: int | None = None,
          run_id: str | None = None,
          limits: Limits | None = None) -> dict:
    """Run a whole batch, synchronously. Returns the final state.

    BLOCKING and network-bound — the caller must hand this to a thread, never
    run it on the relay's event loop. `client` is an OFClient for `account_id`.

    `gate(thunk)` wraps every OnlyFans write this run makes — the carrier post,
    the carrier delete, the final foldering call. Pass it from inside the relay,
    where it routes through the per-account write pacer that automations also
    queue behind. It is a gate rather than a post factory so that `of_client`
    stays the only place that knows what a carrier post looks like.

    Pacing is `limits.pace_s`, and ONLY that — the gate's identity does not
    decide policy. It used to: "no gate injected" meant "sleep locally", so any
    gate that was not the relay's own pacer (a test double, a metrics wrapper)
    silently turned pacing off. The relay passes `Limits(pace_s=0)` because its
    gate already spaces every write for the account; a standalone caller keeps
    the default and gets the local sleep as its backstop.

    `run_id` adopts a run already claimed by `begin()`; without it this claims
    one itself (the CLI path).
    """
    aid = str(account_id)
    limits = (limits or Limits()).checked()
    adopted = run_id is not None
    if not adopted:
        run_id = begin(aid, list_id=list_id)
    state = read_state(run_id)
    if state is None or str(state.get("account_id")) != aid:
        raise RuntimeError(f"vault run {run_id} is not a live run for this account")
    state["status"] = "running"

    try:
        items = _collect(state, local_paths or [], drive_links or [],
                         client=client, limits=limits)
        state["items"] = items
        state["phase"] = "uploading"
        _save(state)

        todo = [i for i in items if i["status"] == "pending"]
        for n, item in enumerate(todo):
            _upload_one(state, item, client, gate, limits)
            _save(state)
            # Space the CARRIERS, and only them. Sleeping after every item —
            # dedupe hits, skipped files, the last file — added ~11s of nothing
            # to each and a 40-file re-run that had nothing to do spent seven
            # minutes asleep. `pace_s` is 0 wherever a gate already spaces the
            # account's writes; see the docstring.
            if (limits.pace_s and n < len(todo) - 1
                    and item.get("carrier_post_id")):
                time.sleep(limits.pace_s)

        _file_into_folder(state, client, gate)
        state["status"] = "done"
    except Exception as e:
        log.exception("vault batch %s failed", run_id)
        state["status"] = "failed"
        # Redacted: this slot is rendered in the dashboard and stored on disk,
        # and a Drive failure that surfaces here is the last error path that had
        # never been scrubbed of the API key.
        state["error"] = gdrive.redact(f"{type(e).__name__}: {e}")
    finally:
        state["phase"] = "finished"
        state["finished_at"] = _now()
        _save(state)
        _cleanup_carriers(state, client, gate)
        _clear_spool(state)
        _save(state)
        with _lock:
            if _live.get(aid) == run_id:
                _live.pop(aid, None)
        _release_lock(_lock_path(aid), run_id)
    return state


# ── collection ────────────────────────────────────────────────────────────

def _collect(state: dict, local_paths: list[str], drive_links: list[str],
             *, client, limits: Limits) -> list[dict]:
    """Build the work list: plan everything first, then fetch what survived.

    The order matters. Deciding what to download only AFTER the caps have been
    applied is what stops a 400 GB folder listing from failing the whole run on
    "not enough disk" when the batch cap meant only 40 files were ever going to
    be fetched.
    """
    def progress(items: list[dict]) -> None:
        state["items"] = items
        _save(state)

    items = _plan(state, local_paths, drive_links, client=client, limits=limits)
    _apply_caps(items, limits)
    progress(items)
    _fetch_drive(items, _run_dir(state["run_id"]), limits, progress)
    return items


def _plan(state: dict, local_paths: list[str], drive_links: list[str],
          *, client, limits: Limits) -> list[dict]:
    """Everything the run could do, with sizes, before a byte moves."""
    items: list[dict] = []

    for p in local_paths:
        path = Path(p)
        size = path.stat().st_size if path.exists() else None
        items.append(_item(path.name, str(path), "local", size, spooled=False))

    if not drive_links:
        return items

    files = gdrive.resolve(drive_links, max_files=resolve_budget(limits))
    for f in files:
        ok, why = gdrive.is_uploadable(f)
        if not ok:
            items.append(_item(f.name, None, "gdrive", f.size,
                               status="skipped", error=why))
            continue
        # Already imported? Recognise it from Drive's own md5, before spending
        # the download. Without this a re-run re-fetches the whole folder just
        # to discover every file is a duplicate.
        if known := _memo_lookup(state["account_id"], f.md5, client):
            it = _item(f.name, None, "gdrive", f.size, status="done")
            it.update(vault_id=known, deduped=True, drive_md5=f.md5)
            items.append(it)
            continue
        it = _item(f.name, None, "gdrive", f.size)
        it.update(drive_id=f.id, drive_md5=f.md5, mime_type=f.mime_type,
                  # Carried so the fetch pass can rebuild the ref: a dropped
                  # resourcekey turns a working public link into a 404.
                  drive_resource_key=f.resource_key)
        items.append(it)
    return items


# How many Drive entries to LIST for a batch of `max_files`. The listing has
# not been filtered yet — Google-native docs, duplicates and oversized files all
# still count — so resolving exactly the cap would let a folder of shared
# spreadsheets consume every slot the operator meant for media. Two-to-one is
# headroom, not a second cap: `_apply_caps` is what actually enforces the batch.
_RESOLVE_HEADROOM = 2


def resolve_budget(limits: Limits) -> int:
    return limits.max_files * _RESOLVE_HEADROOM


def cap_for(name: str, mime: str | None, limits: Limits) -> int:
    """The largest this file may be — the ONE place that rule is decided.

    Video ffmpeg can shrink is allowed up to the (much larger) DOWNLOAD ceiling:
    compression runs later and is the whole reason a 443 MB file can be imported
    at all, and `_upload_one` re-checks the real upload cap once the compressed
    size is known. Everything else is capped at what OnlyFans will actually
    take, since nothing downstream shrinks it.

    It lived in three places — the cap pass, the Drive fetch, and the HTTP
    staging loop, which applied the DOWNLOAD ceiling to everything and so
    streamed a 1.5 GB dragged-in JPEG to disk in full before skipping it at
    200 MB. Callers ask here instead.
    """
    return (limits.max_download_bytes
            if media_prep.can_shrink(name, mime) else limits.max_bytes)


def _apply_caps(items: list[dict], limits: Limits) -> None:
    """The size and batch caps, in ONE pass over the plan.

    There were two implementations of this rule with the same error string, on
    either side of the Drive download, and they disagreed about oversized video:
    one let it through to be compressed without ever counting it against the
    batch cap, so 40 small files plus 20 large videos processed 60.
    """
    pending = 0
    for it in items:
        if it["status"] != "pending":
            continue
        shrinkable = media_prep.can_shrink(it["name"], it.get("mime_type"))
        cap = cap_for(it["name"], it.get("mime_type"), limits)
        if it["size"] is not None and it["size"] > cap:
            it.update(status="skipped",
                      error=f"{it['size'] / 1e6:.0f} MB exceeds the "
                            f"{cap / 1e6:.0f} MB limit"
                            + ("" if shrinkable else " and cannot be compressed"))
            continue
        pending += 1
        if pending > limits.max_files:
            it.update(status="skipped",
                      error=f"batch cap of {limits.max_files} files reached")


def _fetch_drive(items: list[dict], run_dir: Path, limits: Limits,
                 progress: Callable[[list[dict]], None]) -> None:
    """Download the Drive files the caps left standing, one at a time.

    Free space is checked before EACH file rather than once against the sum of
    the whole listing: the sum included files the cap was always going to skip,
    and a folder far bigger than the batch failed the run before a byte moved.
    """
    for it in items:
        if it["status"] != "pending" or it["source"] != "gdrive" or it.get("path"):
            continue
        try:
            guard_space(it["size"] or 0, limits, path=run_dir)
            f = gdrive.DriveFile(id=it["drive_id"], name=it["name"],
                                 mime_type=it.get("mime_type") or "",
                                 size=it["size"],
                                 resource_key=it.get("drive_resource_key"),
                                 md5=it.get("drive_md5"))
            dest = gdrive.download(
                f, run_dir,
                max_bytes=cap_for(it["name"], it.get("mime_type"), limits),
                # Drive's declared size is missing for some items, so the guard
                # above can be guarding zero bytes; the download re-checks free
                # space as it streams rather than trusting the metadata.
                min_free_bytes=limits.min_free_bytes)
            it.update(path=str(dest), spooled=True)
        except Exception as e:  # noqa: BLE001 — one bad file must not end the run
            it.update(status="failed", error=gdrive.redact(f"{type(e).__name__}: {e}"))
        progress(items)


def _item(name, path, source, size, *, status="pending", error=None,
          spooled=False) -> dict:
    """The item schema. EVERY key an item can carry is declared here — the
    dashboard's row type is written against this and nothing else."""
    return {"name": name, "path": path, "source": source, "size": size,
            "status": status, "error": error, "spooled": spooled,
            "vault_id": None, "carrier_post_id": None, "carrier_deleted": None,
            # Two different claims, deliberately two fields. `carrier_deleted`
            # means "the post whose id is above is gone from OnlyFans" — and a
            # 404 on the delete proves that as well as a 200 does.
            # `carrier_scan_clear` means "a marker scan that ran to completion
            # found no post for this intent". Saying the second with the first
            # asserted a deletion that never happened, on a field the CLI and
            # the dashboard both read.
            "carrier_intent": None, "carrier_scan_clear": None,
            "carrier_write_pending": None,
            "deduped": None, "hidden": None,
            "still_transcoding": None,
            "compressed_from": None, "claim_retries": 0,
            "drive_id": None, "drive_md5": None, "mime_type": None,
            "drive_resource_key": None}


def guard_space(incoming: int, limits: Limits | None = None, *,
                path: Path | None = None) -> None:
    """Refuse to write `incoming` bytes if it would eat the free-space floor.

    `path` names the filesystem to measure — the staging directory for an HTTP
    upload is often NOT on the same volume as the spool, and measuring the wrong
    one is the same as not measuring.
    """
    limits = limits or Limits()
    target = Path(path or SPOOL_ROOT)
    target.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(target).free
    if free - incoming < limits.min_free_bytes:
        raise RuntimeError(
            f"not enough disk: {free / 1e9:.1f} GB free, this needs "
            f"~{incoming / 1e9:.1f} GB and "
            f"{limits.min_free_bytes / 1e9:.1f} GB must stay free")


# ── one file ──────────────────────────────────────────────────────────────

def _upload_one(state: dict, item: dict, client, gate, limits: Limits) -> None:
    """One file: upload, mint a vault id, drop the spooled copy."""
    path = item.get("path")
    if not path or not Path(path).exists():
        item.update(status="failed",
                    error=("file missing from spool" if item.get("spooled")
                           else "file no longer exists on disk"))
        return

    def note_carrier_intent(marker: str) -> None:
        # Written BEFORE the create call goes out. If the response is lost — a
        # timeout on a slow attach, a proxy error, a SIGKILL — OnlyFans may have
        # created the post anyway and we will never learn its id. This record is
        # what tells sweep() to go looking for a post carrying `marker`.
        item["carrier_intent"] = {"at": _now(), "marker": marker}
        item["carrier_deleted"] = False
        _save(state)

    def remember_carrier(post_id) -> None:
        # Before the poll, before anything else — this narrows the sweep from
        # "a marker-bearing post" to an exact id.
        item["carrier_post_id"] = post_id
        item["carrier_deleted"] = False
        _save(state)

    def carrier_gone(post_id) -> None:
        # Fired from of_client's `finally`, so the record is right even when the
        # call that owned it is on its way out with an exception. The return
        # value cannot say this on that path, and an item left claiming a live
        # carrier that OnlyFans has actually deleted is a red banner nothing can
        # ever clear.
        item["carrier_post_id"] = post_id
        item["carrier_deleted"] = True
        _save(state)

    # Shrink oversized video first. OF's convert step answers 504 on large
    # objects, so this is not an optimisation — above the ceiling it is the
    # difference between an upload that lands and one that cannot.
    #
    # ffmpeg writes its output NEXT TO a source that may already be 2 GB, so the
    # free-space floor is checked once more here — the earlier guard covered the
    # download, not the re-encode it feeds.
    try:
        if media_prep.can_shrink(item["name"], item.get("mime_type")):
            guard_space(Path(path).stat().st_size, limits,
                        path=_run_dir(state["run_id"]))
    except Exception as e:  # noqa: BLE001 — one file, not the whole run
        item.update(status="failed", error=f"{type(e).__name__}: {e}")
        return
    upload_path, compressed = media_prep.prepare(
        path, _run_dir(state["run_id"]),
        over_mb=limits.compress_over_mb, target_mb=limits.target_mb,
        mime=item.get("mime_type"))
    if compressed:
        item["compressed_from"] = item["size"]
        item["size"] = Path(upload_path).stat().st_size
        _save(state)

    # Now that the real upload size is known, enforce the ceiling. Above it,
    # OnlyFans' convert step answers a hard 504 AFTER every byte has been
    # stored, so refusing here saves a pointless multi-minute upload.
    final_size = Path(upload_path).stat().st_size
    if final_size > limits.max_bytes:
        item.update(status="skipped",
                    error=(f"{final_size / 1e6:.0f} MB is over the "
                           f"{limits.max_bytes / 1e6:.0f} MB limit"
                           + (" even after compression" if compressed else "")
                           + " — OnlyFans rejects objects this large"))
        if compressed and str(upload_path) != str(path):
            Path(upload_path).unlink(missing_ok=True)
        return

    item["status"] = "uploading"
    _save(state)
    try:
        res = client.upload_to_vault(str(upload_path),
                                     on_carrier=remember_carrier,
                                     on_carrier_intent=note_carrier_intent,
                                     on_carrier_deleted=carrier_gone,
                                     gate=gate)
        item["vault_id"] = res.get("vault_id")
        item["deduped"] = bool(res.get("deduped"))
        item["hidden"] = bool(res.get("hidden"))
        item["claim_retries"] = res.get("claim_retries") or 0
        item["still_transcoding"] = bool(res.get("still_transcoding"))
        if res.get("carrier_post_id"):
            item["carrier_post_id"] = res["carrier_post_id"]
            item["carrier_deleted"] = bool(res.get("carrier_deleted"))
        # There is deliberately no "clear the intent" branch here. An intent is
        # only ever written from inside `materialize_to_vault`, which then
        # either returns an id or raises — a dedupe hit short-circuits earlier
        # and never fires the hook at all. Clearing an intent on a guess would
        # mean deciding, from no evidence, that a carrier we cannot see does not
        # exist; only a completed marker scan is allowed to conclude that.
        if res.get("hidden"):
            # The bytes ARE on OnlyFans, under an id the creator removed —
            # hiding is OF's only delete and it is one-way, so re-uploading the
            # same file can never restore it. Calling that "done" is how twenty
            # photos are lost with a green tick beside each one.
            item.update(status="skipped",
                        error="already in the vault but HIDDEN (removed there) — "
                              "OnlyFans dedupes on the bytes, so re-uploading "
                              "cannot bring it back")
        elif res.get("vault_id"):
            item["status"] = "done"
        else:
            item.update(status="failed", error=res.get("note") or "no vault id")
    except Exception as e:
        log.exception("vault upload failed for %s", item["name"])
        item.update(status="failed", error=f"{type(e).__name__}: {e}")
        if isinstance(e, FuturesTimeout):
            # The write gate gave up waiting. The route cancels the queued
            # coroutine, but a cancel that loses the race still creates the
            # post — so this intent cannot be scanned for: a marker scan run
            # NOW would correctly find nothing and wrongly conclude the
            # carrier never existed. Leave it outstanding; the sweep keeps
            # looking, and retention keeps the record.
            item["carrier_write_pending"] = True

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


def _gate(gate):
    """`gate`, or a pass-through. One spelling of "run this write through the
    caller's pacer if there is one", instead of the same lambda three times."""
    return gate or (lambda thunk: thunk())


def _file_into_folder(state: dict, client, gate=None) -> None:
    """Put everything that succeeded into the target vault list, in one call."""
    list_id = state.get("list_id")
    ids = [i["vault_id"] for i in state["items"]
           if i.get("vault_id") and i["status"] == "done"]
    if not list_id or not ids:
        return
    run = _gate(gate)
    try:
        run(lambda: client.add_media_to_vault_list(int(list_id), ids))
        state["filed_into_list"] = len(ids)
    except Exception as e:
        # Never fatal, and never a reason to re-upload: the media is already in
        # the vault, just not in the folder the operator picked. It gets its own
        # field — writing it into `error` painted a successful import red.
        log.warning("vault batch %s: foldering failed — %s", state["run_id"], e)
        state["filed_into_list"] = 0
        state["filing_error"] = f"could not file into the vault folder: {e}"


def _cleanup_carriers(state: dict, client, gate=None) -> None:
    """Delete any carrier still recorded as live. Normally a no-op — the
    per-file path deletes its own — but this is what catches the ones a
    mid-run exception skipped."""
    run = _gate(gate)
    for it in state["items"]:
        pid = it.get("carrier_post_id")
        if not pid or it.get("carrier_deleted"):
            continue
        try:
            run(lambda p=pid: client.delete_post(p))
            it["carrier_deleted"] = True
        except Exception as e:
            # A post OnlyFans says does not exist cannot publish, so a 404 here
            # is this having already worked — most often by the `finally` in
            # materialize_to_vault, whose caller then raised before the fact
            # could be recorded. Calling it a failure left a permanent alarm.
            if of_client.post_is_gone(e):
                it["carrier_deleted"] = True
                log.info("vault: carrier %s (%s) was already gone",
                         pid, it.get("name"))
                continue
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

def _marker_text(post: dict) -> str:
    """A scheduled post's text, with OF's markup out of the way."""
    raw = post.get("rawText") or post.get("text") or ""
    return html.unescape(re.sub(r"<[^>]+>", "", str(raw)))


def _known_carriers(account_id: str) -> set[str]:
    """Every carrier id this account has on record RIGHT NOW.

    Read fresh, never from a snapshot: an id learned since the sweep started is
    exactly the one a live import is polling on, and the marker scan's rule is
    "delete what nobody has recorded".
    """
    aid, out = str(account_id), set()
    for d in _dirs():
        try:
            raw = json.loads((d / "run.json").read_text())
        except (OSError, ValueError):
            continue
        if str(raw.get("account_id")) != aid:
            continue
        for i in raw.get("items") or []:
            if i.get("carrier_post_id"):
                out.add(str(i["carrier_post_id"]))
    return out


_SCHEDULE_PAGE = 100
_SCHEDULE_MAX_PAGES = 50


def _sweep_scheduled(client, account_id: str,
                     recheck: Callable[[], set | None]) -> tuple[int, int, bool]:
    """Reconcile against OnlyFans' OWN list of scheduled posts, DELETING the
    ones nobody has on record.

    The run record cannot be the only source of truth here. If the create call's
    response was lost — a timeout while OF attached a 180 MB video, a proxy
    error on the last retry — the post exists and its id was never learned, so
    nothing in `run.json` can point at it. The marker is the handle: no other
    writer on the account ever produces that text, so a scheduled post carrying
    it belongs to an import, and one whose id no live run owns is deletable.

    PAGED to exhaustion, and the third return value says whether the listing
    actually completed. A single page of 100 never reached a carrier scheduled
    30 days out on a creator with a long queue, and a listing that FAILED
    returned the same `(0, 0)` as a listing that found nothing — "we looked and
    it is clean" and "we could not look" reported identically, on the only
    recovery path an unrecorded carrier has.

    `recheck()` returns the account's carrier ids as they are AT THAT MOMENT, or
    None if an import has started since. Both matter, and a frozen snapshot of
    either is the bug this walk keeps re-growing: paging a long queue is up to
    fifty sequential round trips, tens of seconds, and an import that begins
    inside that window creates a carrier which is (a) genuinely live and (b) not
    in any set taken before it existed. So it is asked again before the listing
    and again before every single delete.

    Returns `(deleted, still_live, listed_completely)`. Abandoning because the
    account went live is NOT a complete scan: nothing may be retired on it.
    """
    known = recheck()
    if known is None:
        log.info("vault sweep: account %s started importing — leaving its "
                 "scheduled posts alone", account_id)
        return 0, 0, False
    rows: list = []
    for page in range(_SCHEDULE_MAX_PAGES):
        try:
            resp = client.schedules_later_post(limit=_SCHEDULE_PAGE,
                                               offset=page * _SCHEDULE_PAGE)
        except Exception as e:  # noqa: BLE001 — a listing failure is not a run failure
            log.error("vault sweep: could NOT list scheduled posts for account "
                      "%s — an unrecorded carrier may be live: %s", account_id, e)
            return 0, 1, False
        batch = resp.get("list") if isinstance(resp, dict) else resp
        batch = list(batch or [])
        rows.extend(batch)
        more = resp.get("hasMore") if isinstance(resp, dict) else None
        if not batch or len(batch) < _SCHEDULE_PAGE or more is False:
            break
    else:
        log.error("vault sweep: scheduled-post listing for account %s did not "
                  "end after %d pages — treating the scan as incomplete",
                  account_id, _SCHEDULE_MAX_PAGES)
        return 0, 1, False

    deleted = failed = 0
    for post in rows:
        pid = post.get("id") if isinstance(post, dict) else None
        if pid is None or str(pid) in known:
            continue
        if of_client.VAULT_MARKER not in _marker_text(post):
            continue
        # Re-asked per candidate, immediately before the irreversible bit. The
        # listing above may be a minute old by now.
        fresh = recheck()
        if fresh is None:
            log.info("vault sweep: account %s started importing mid-scan — "
                     "abandoning it, nothing retired", account_id)
            return deleted, failed, False
        known = fresh
        if str(pid) in known:
            continue
        try:
            client.delete_post(pid)
            deleted += 1
            log.warning("vault sweep: deleted an UNRECORDED carrier post %s — "
                        "its create response must have been lost", pid)
        except Exception as e:  # noqa: BLE001
            failed += 1
            log.error("vault sweep: unrecorded carrier %s STILL LIVE — %s", pid, e)
    return deleted, failed, failed == 0


def sweep(client_for: Callable[[str], Any], *, keep_hours: int = 48) -> dict:
    """Delete orphaned carriers, bin stale run dirs.

    Runs at boot AND on a timer: the relay is a long-lived container that may
    not restart for weeks, and a carrier publishes 30 days out, so "we clean up
    next boot" is not a schedule.

    `client_for(account_id)` supplies an OFClient. This is the piece the
    reference implementation has no equivalent of, and the reason a crash here
    costs nothing worse than a re-run.

    THREE PASSES, and the order is the whole design:

      1. read every run record and write NOTHING, collecting the accounts that
         hold an unresolved intent. A run that is still going owns its carriers
         — one legitimately exists, recorded and undeleted, for the entire
         ten-minute transcode poll — and an account with ANY importer is left
         alone whole: an hourly sweep can wait an hour, and guessing which of a
         busy account's carriers are orphaned is the guess that deletes a live
         one.
      2. ask OnlyFans what is actually scheduled for those accounts, and DELETE
         the marker-bearing posts nobody has on record. This pass writes to
         OnlyFans; saying it only "asks" is how the liveness guard came to be
         missing from it for two rounds. Liveness and the recorded-carrier set
         are re-read inside it, per delete — pass 1's answers are a snapshot,
         and this pass can run for minutes.
      3. only now delete recorded carriers, write records, and retire
         directories — and only ever for runs pass 1 found dead.

    Merging 2 into 3 is what made retention destroy an intent-only record before
    anything had successfully looked for the post it describes.
    """
    fixed, failed, removed = 0, 0, 0
    cutoff = time.time() - keep_hours * 3600
    # Accounts to reconcile against OnlyFans itself. The carrier ids are NOT
    # collected here: they are re-read inside pass 2, at the moment of use.
    scan: set[str] = set()
    dead: list[tuple[Path, dict, bool]] = []
    live_cache: dict[str, bool] = {}

    # Staging directories for HTTP uploads live under the spool (not TMPDIR)
    # precisely so this sweep can own the ones a SIGKILL left behind.
    for d in sorted(STAGE_ROOT.glob("vault-in-*")) if STAGE_ROOT.is_dir() else []:
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            continue

    # ── pass 1: read ──────────────────────────────────────────────────────
    for d in _dirs():
        try:
            raw = json.loads((d / "run.json").read_text())
        except (OSError, ValueError):
            # No state file — nothing can be recovered from it, and its media
            # is unreferenced. Bin it if it's old enough to be certainly dead.
            try:
                stale = d.stat().st_mtime < cutoff
            except OSError:
                stale = False
            if stale:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
            continue
        state = _reconcile(raw, live_cache)
        aid = str(state.get("account_id"))
        items = state.get("items") or []
        # The whole account, not just its live run — see the docstring.
        if _account_is_live(aid, live_cache):
            continue
        dead.append((d, state, state.get("status") != raw.get("status")))
        if any(_intent_unresolved(i) for i in items):
            scan.add(aid)

    # ── pass 2: what does OnlyFans itself say is scheduled? Delete it. ────
    scanned: set[str] = set()
    for aid in sorted(scan):
        def _recheck(aid=aid) -> set | None:
            """Fresh liveness AND fresh carrier ids, together, at the moment of
            use. Deliberately uncached: this is the answer pass 1's cache is too
            old to give."""
            return None if _account_is_live(aid) else _known_carriers(aid)

        if _recheck() is None:
            continue
        try:
            client = client_for(aid)
        except Exception as e:  # noqa: BLE001
            log.error("vault sweep: no client for account %s — %s", aid, e)
            failed += 1
            continue
        got, lost, complete = _sweep_scheduled(client, aid, _recheck)
        fixed += got
        failed += lost
        if complete:
            scanned.add(aid)

    # ── pass 3: delete, record, retire ────────────────────────────────────
    # Liveness re-asked once more, and freshly: pass 2 can run for minutes, and
    # the rule is the same one — an account with an importer is left alone.
    now_cache: dict[str, bool] = {}
    for d, state, changed in dead:
        aid = str(state.get("account_id"))
        if _account_is_live(aid, now_cache):
            continue
        items = state.get("items") or []
        live = [i for i in items
                if i.get("carrier_post_id") and not i.get("carrier_deleted")]
        if live:
            try:
                client = client_for(aid)
                for it in live:
                    try:
                        client.delete_post(it["carrier_post_id"])
                        it["carrier_deleted"] = True
                        changed = True
                        fixed += 1
                        log.warning("vault sweep: deleted orphaned carrier %s",
                                    it["carrier_post_id"])
                    except Exception as e:
                        # 404 means somebody already deleted it — most often the
                        # `finally` whose caller raised before it could record
                        # the fact. Gone is gone; it cannot publish.
                        if of_client.post_is_gone(e):
                            it["carrier_deleted"] = True
                            changed = True
                            continue
                        failed += 1
                        log.error("vault sweep: carrier %s STILL LIVE — %s",
                                  it["carrier_post_id"], e)
            except Exception as e:
                failed += len(live)
                log.error("vault sweep: no client for account %s — %s", aid, e)
        # An intent is only ever retired by a scan that actually completed.
        # Anything less — a listing that failed, one we could not page to the
        # end of, one abandoned because an import started — leaves it exactly
        # where it is, for the next sweep. The intent record itself is KEPT: it
        # is the evidence, and `carrier_scan_clear` is the conclusion drawn
        # about it. Writing that conclusion into `carrier_deleted` asserted a
        # deletion that never happened, on a field the CLI and the dashboard
        # both read as "a post we named is gone".
        if aid in scanned:
            for it in items:
                if _intent_unresolved(it):
                    it["carrier_scan_clear"] = True
                    changed = True
        if changed:
            _save(state)

        # Retention. Stat the STATE FILE, not the directory: `_save` writes via
        # mkstemp + os.replace inside the run dir, which bumps the directory's
        # mtime every time — so a directory mtime is never older than the last
        # write and nothing was ever removed. A run with an unresolved carrier
        # is kept regardless; its record is the only thing that can clean it up
        # — and that includes an INTENT with no id, which is the MOST dangerous
        # record there is, not the least: nothing else knows the post may exist.
        unresolved = any(
            (i.get("carrier_post_id") and not i.get("carrier_deleted"))
            or _intent_unresolved(i)
            for i in items)
        try:
            stale = (d / "run.json").stat().st_mtime < cutoff
        except OSError:
            stale = False
        if state.get("finished_at") and stale and not unresolved:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1

    if fixed or failed or removed:
        log.info("vault sweep: %d carriers deleted, %d still live, %d dirs removed",
                 fixed, failed, removed)
    return {"carriers_deleted": fixed, "carriers_failed": failed,
            "dirs_removed": removed}
