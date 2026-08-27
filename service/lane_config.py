"""service/lane_config.py — the durable store behind the Concurrency panel.

WHY THIS EXISTS. Every concurrency ceiling in the relay was an env var, which
means changing one meant editing an env file (or a compose override that the
deploy rsync excludes) and redeploying. That is enough friction that the numbers
never got retuned — the exact failure `admission.py`'s docstring names: "a
ceiling nobody can read the current value of is a ceiling nobody maintains".
This module lets the panel WRITE them.

PRECEDENCE, and why it is this way::

    env var  >  stored override  >  code default

The env var wins on purpose. It is the lockout escape hatch: if a value saved
from the panel makes the process unhealthy, `docker run -e IMG_FETCH_CONCURRENCY=8`
recovers it without anyone having to find and hand-edit a JSON file inside a
container. Same reasoning as `ALLOW_ANONYMOUS_ADMIN` — a bad state must be one
restart away from fixed, never a redeploy.

🚨 CHANGES APPLY AT RESTART, NOT LIVE, and the panel says so. This is not
laziness — it is forced. Every ceiling here is a semaphore constructed exactly
once: `Lane.__init__` builds a `threading.BoundedSemaphore(cap)`, the automation
executor builds its `asyncio.Semaphore` when the supervisor starts, and
`LoopLane._semaphore()` says it outright ("what makes it useless as a runtime
knob"). A `ThreadPoolExecutor` cannot be resized at all. Mutating `.cap` after
the fact would change the NUMBER THE PANEL DISPLAYS and not the ceiling being
enforced, which is worse than not having the panel.

THE REGISTRY IS DERIVED, NOT DECLARED, and that is the whole design. There is no
table of knobs in this file. `resolve()` is called once per ceiling, at import,
by the code that OWNS that ceiling — and that call already carries both facts a
panel needs: the env var's name, and the default the owner chose. So `resolve()`
records them, and `describe()` is built from what actually got resolved.

That inversion is load-bearing, because the alternative had two ways to lie. A
declared table restates a default the call site already owns, so changing one
makes the panel report the other. And a hand-written map of "live" values means
a knob added in one place and missed in the other reports `live=None`, which
computes to `pending=False` — the "restart to apply" banner silently never
appears for the one knob that needed it. Neither failure is possible here: a
ceiling nothing resolves is not in the panel, and a ceiling something resolves
cannot be missing from it.

The live value needs no separate bookkeeping either. Nothing mutates these after
import — `Lane.cap`, `_EXECUTOR_THREADS`, `_MAX_CONCURRENT_RUNS` are all set once
and read forever — so what `resolve()` returned the FIRST time it was asked IS
what this process is enforcing. `record_live()` exists for the single call site
that clamps a value after resolving it (the background sub-cap, which is pinned
below total); it is the exception that would otherwise make `_live` subtly wrong.

WHERE IT LIVES, and why not `service/secrets/`. The obvious home was the
secrets directory — it is bind-mounted and already holds this shape of state.
It is also, on the current stack, mounted from a path under `/private/tmp`,
which macOS clears on reboot. Settings that silently revert are worse than
settings you cannot change, so this writes alongside the SQLite DB instead:
that is a real docker volume and the one directory in the container that
actually survives. `LANE_CONFIG_PATH` overrides if that ever stops being true.

A LEAF, like `admission`. It imports nothing from the relay, because the lanes
it feeds are constructed at MODULE IMPORT — long before the event loop exists
or `db_init()` has run. That is also why every read here is sync: there is no
loop to await on at the moment the value is needed.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from atomic_json import write_atomic

log = logging.getLogger("of-relay.lane-config")

_HERE = Path(__file__).resolve().parent


def _default_path() -> Path:
    """The durable file. See the module docstring for why not `secrets/`."""
    explicit = os.environ.get("LANE_CONFIG_PATH", "").strip()
    if explicit:
        return Path(explicit)
    # Alongside the SQLite file — the docker volume, i.e. the one mount that is
    # not under /private/tmp. Parsed rather than assumed so a Postgres URL or a
    # relocated DB does not silently drop us in an ephemeral directory.
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("sqlite") and "///" in url:
        db_file = Path(url.split("///", 1)[1].split("?", 1)[0])
        if db_file.parent.is_dir():
            return db_file.parent / "lane_config.json"
    if (_HERE / "dbdata").is_dir():
        return _HERE / "dbdata" / "lane_config.json"
    return _HERE / "lane_config.json"


_PATH = _default_path()
# The audit trail behind the panel's history list — one JSON object per line,
# appended on every saved change. Lives beside the store for the same
# durability reasoning; losing it loses the story, never the settings.
_HISTORY = _PATH.with_name("lane_config_history.jsonl")

_lock = threading.Lock()
_stored_cache: dict[str, int] | None = None

# Filled by `resolve()`. See the module docstring: these are the registry, and
# nothing else declares one.
_defaults: dict[str, int] = {}   # env var -> the default its owner passed
_live: dict[str, int] = {}       # env var -> what this process is enforcing


def _clamp(value: Any) -> int:
    """The one legality rule, shared by every path in and out of this module.

    Floor of 1 and integer-only, which is exactly what the `max(1, int(...))`
    at each call site did before it moved here — so adopting this changed where
    a number comes from and never what counts as a legal number. There is no
    upper bound on purpose: the operator asked for the ceiling to be theirs to
    set, and a silent clamp would be a cap wearing a text box's clothes.
    """
    return max(1, int(value))


def _load() -> dict[str, int]:
    """Read the file. Never raises: a missing or corrupt store must boot the
    process on code defaults, not refuse to boot. A ceiling that fails closed
    at import is an outage, and the value it is protecting is a tuning number."""
    try:
        raw = json.loads(_PATH.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        log.warning("lane_config unreadable at %s — using code defaults", _PATH,
                    exc_info=True)
        return {}
    if not isinstance(raw, dict):
        log.warning("lane_config is not an object — using code defaults")
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        try:
            out[key] = _clamp(value)
        except (TypeError, ValueError):
            log.warning("lane_config: %s=%r is not an int — ignoring", key, value)
    return out


def stored() -> dict[str, int]:
    """Every override currently on disk. Empty dict if nothing was ever set.

    Unknown keys are kept rather than dropped. A knob whose owning module has
    not been imported yet is indistinguishable from one that was retired, and
    silently deleting the first on the next save is how a setting disappears
    for reasons nobody can reconstruct.
    """
    global _stored_cache
    with _lock:
        if _stored_cache is None:
            _stored_cache = _load()
        return dict(_stored_cache)


def _from_env(env_var: str) -> int | None:
    """The env layer of the precedence chain, in ONE place.

    `resolve()` and `source()` both have to answer "is an env var pinning this?"
    and they have to agree. Two copies of this parse is how the panel ends up
    disabling an input that is not actually pinned — or worse, accepting an edit
    it cannot deliver.
    """
    raw = os.environ.get(env_var)
    if raw is None or not str(raw).strip():
        return None
    try:
        return _clamp(raw)
    except (TypeError, ValueError):
        log.warning("%s=%r is not an int — falling through to stored/default",
                    env_var, raw)
        return None


def _current(env_var: str, default: int) -> tuple[int, str]:
    """(value, source) under the precedence in the module docstring. The single
    implementation of that chain — everything public here is a view onto it."""
    from_env = _from_env(env_var)
    if from_env is not None:
        return from_env, "env"
    from_store = stored().get(env_var)
    if from_store is not None:
        return _clamp(from_store), "stored"
    return _clamp(default), "default"


def resolve(env_var: str, default: int) -> int:
    """The one function the ceilings call — and the registration that makes the
    panel possible. See the module docstring on why the registry is derived.

    The FIRST resolution of a knob is recorded as its live value, because these
    are read once at import and never mutated. Later calls (the panel
    re-reading, a test) recompute freely without disturbing that.
    """
    value, _ = _current(env_var, default)
    _defaults[env_var] = _clamp(default)
    _live.setdefault(env_var, value)
    return value


def record_live(env_var: str, value: int) -> None:
    """Correct the recorded live value for a knob that is post-processed after
    `resolve()` returns.

    Exactly one call site needs this: `ACCOUNT_LANE_BACKGROUND` is clamped below
    `ACCOUNT_LANE_TOTAL` after resolving, so the number being enforced is not
    always the number resolved. Without this the panel would report a pending
    change that will never happen, which is the specific lie it exists to avoid.
    """
    _live[env_var] = _clamp(value)


def knob_names() -> frozenset[str]:
    """Every knob this process actually reads — the write path's allowlist.

    Derived, so it cannot drift from the ceilings that exist. A name absent here
    is a name nothing would consult, and storing it would be writing to a key no
    boot will ever read.
    """
    return frozenset(_defaults)


def set_many(values: dict[str, Any]) -> dict[str, int]:
    """Persist overrides and return the full stored set.

    Atomic — temp file in the same directory, then `os.replace`. A half-written
    JSON here would boot the whole process on code defaults (see `_load`), and
    an interrupted save is exactly when you would least want the ceilings to
    quietly move.

    A value of `None` DELETES the override, which is how the panel offers
    "back to default" without needing a second endpoint.
    """
    known = knob_names()
    clean: dict[str, int | None] = {}
    for name, value in values.items():
        if name not in known:
            raise ValueError(f"unknown knob: {name}")
        if value is None:
            clean[name] = None
            continue
        try:
            clean[name] = _clamp(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be an integer, got {value!r}")

    global _stored_cache
    with _lock:
        current = dict(_stored_cache) if _stored_cache is not None else _load()
        changes: list[dict[str, Any]] = []
        for name, value in clean.items():
            before = current.get(name)
            if value is None:
                current.pop(name, None)
            else:
                current[name] = value
            if before != value:
                changes.append({
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "key": name,
                    "old": before,
                    "new": value,
                })
        _write(current)
        _stored_cache = dict(current)
        _append_history(changes)
        log.info("lane_config saved %s -> %s", sorted(clean), _PATH)
        return dict(current)


def _write(data: dict[str, int]) -> None:
    """Atomic replace of the store. Caller holds `_lock`."""
    write_atomic(_PATH, data)


def _append_history(changes: list[dict[str, Any]]) -> None:
    """Best-effort audit trail behind the panel's history list. Caller holds
    `_lock`. A failed append must never fail the save it describes — the store
    is the record that matters; this is the story of how it got there."""
    if not changes:
        return
    try:
        _HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with open(_HISTORY, "a+b") as fh:
            # Heal a torn tail before appending: a crash or full disk can leave
            # the file ending mid-line, and appending straight after it would
            # glue THIS row onto the fragment — losing a change that really
            # happened, not just the one the crash already lost.
            fh.seek(0, os.SEEK_END)
            if fh.tell() > 0:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    fh.write(b"\n")
            for row in changes:
                fh.write((json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))
    except OSError:
        log.warning("lane_config history append failed at %s", _HISTORY,
                    exc_info=True)


def note_restart() -> None:
    """Append the one row that is not a knob change: the relay was restarted
    from the panel.

    It belongs in this file rather than a second log because the story the panel
    tells is a sequence: a cap changed, and then it was applied. Splitting those
    across two files would show the operator half of it. Rows carry `event`
    instead of `key`/`old`/`new`, and `history()` returns both shapes; the panel
    branches on which one it got.

    Takes no argument on purpose. A free-text sink with exactly one caller
    invites a second caller with a different phrasing, and the history is read
    by a UI that has to branch on the shape.
    """
    with _lock:
        _append_history([{
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": "relay restarted from the panel",
        }])


def history(limit: int = 50) -> list[dict[str, Any]]:
    """The most recent saved changes, newest first. Never raises — a missing or
    torn file reads as an empty history, the same stance `_load` takes: the
    panel must render on a fresh box where nothing was ever changed."""
    try:
        lines = _HISTORY.read_text("utf-8").splitlines()
    except FileNotFoundError:
        return []
    except Exception:
        # Same breadth as _load, for the same reason — and specifically:
        # UnicodeDecodeError is a ValueError, not an OSError, and one byte of
        # block-level corruption here must not 500 the panel's GET and POST.
        log.warning("lane_config history unreadable at %s", _HISTORY,
                    exc_info=True)
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines[-max(1, int(limit)):]):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def describe() -> list[dict[str, Any]]:
    """Every knob as a row, for the panel: what the next boot will use, what is
    running now, where the value came from, and whether those disagree.

    `pending` is the field the whole panel turns on — a value saved but not yet
    adopted. It is computed here rather than in the UI so "saved but not
    running" has one definition.
    """
    rows: list[dict[str, Any]] = []
    saved = stored()
    for env_var in sorted(_defaults):
        default = _defaults[env_var]
        value, src = _current(env_var, default)
        live = _live.get(env_var)
        rows.append({
            "name": env_var,
            "value": value,
            "default": default,
            "stored": saved.get(env_var),
            "source": src,
            "env_locked": src == "env",
            "live": live,
            "pending": live is not None and live != value,
        })
    return rows


def path() -> str:
    """Where the file is, for the panel's footer. Operators need to know which
    file to delete when they want a clean slate."""
    return str(_PATH)
