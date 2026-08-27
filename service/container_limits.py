"""service/container_limits.py — what the CONTAINER is limited to, read from cgroup.

WHY THIS IS ITS OWN MODULE. It answers a question about the machine, not about
this application: how many CPUs and how much memory the kernel will let this
container have. It shares no state with the concurrency store it is rendered
beside — no lock, no file, no precedence chain — and nothing here is durable or
stored. It lived in `lane_config` for exactly one bad reason: the same web page
shows both. `lane_config` is imported at module scope by every ceiling in the
relay (`admission`, `server`, `vault_stills`, `automation_executor`), and none
of them will ever call a cgroup parser.

🚨 READ ONLY, AND THAT IS NOT A SHORTCUT. CPU and memory are the two ceilings
operators reach for first, and the two this process categorically cannot set.
They belong to the container, not the code inside it, and every route in is
closed (verified 2026-08-27 against the live relay): `/sys/fs/cgroup` is mounted
`ro`, so writing `cpu.max` answers EROFS, and the image carries neither a docker
socket nor a docker CLI to call `docker update` with.

🚨 Mounting `/var/run/docker.sock` to close that gap was considered and
REJECTED. The panel that renders this is deliberately unauthenticated, and that
socket is root on the host — the mount would turn "anyone who can reach the port
may retune a cap" into "anyone who can reach the port owns the machine". A
restriction that only holds because the door is locked is not a restriction; do
not add the mount to make this module writable.

So the panel READS what is enforced and hands the operator the exact command.
Note what is deliberately NOT here: a stored "desired cpus". This process could
never deliver one, so it would sit `pending` forever and become a second source
of truth for a number the cgroup already answers exactly.

THE ONE WRONG ANSWER THIS MODULE CAN GIVE is "no limit" for a file it merely
failed to open. "Unlimited" and "could not tell" send an operator to opposite
conclusions — one says there is headroom, the other says go look. So every
reader returns `(value, readable)` and NEVER a bare `None`, and the two readers
report their own `readable` separately: a box can have a perfectly readable
`cpu.max` and an unparseable `memory.max`, and memory is the one that matters
most to get right, because exceeding it is an OOM-kill rather than a slowdown.

A LEAF: imports only `os` and `pathlib`, and never raises. It feeds a page, and
a box whose cgroup files are missing must render "unknown" rather than 500 the
panel that shows every other ceiling.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Module-level so tests can point them somewhere harmless. `_HOSTNAME` is here
# for the same reason and not inlined: it feeds a `docker update <id>` command
# an operator pastes into a root shell, so the strip-and-truncate that keeps a
# stray newline or a 64-char id out of that line has to be testable.
_CGROUP = Path("/sys/fs/cgroup")
_HOSTNAME = Path("/etc/hostname")

# cgroup v1 spells "no limit" as a sentinel near 2**63 rather than "max". Any
# reading above this is that sentinel, not a real ceiling — rendering it
# literally would claim a ~9 exabyte memory limit.
_V1_UNLIMITED = 1 << 62


def _read_first(*paths: Path) -> str | None:
    """First of these files that reads, stripped. None if none of them do,
    which is the honest answer off Linux or outside a cgroup — and is NOT the
    same answer as "no limit"."""
    for candidate in paths:
        try:
            return candidate.read_text("utf-8").strip()
        except (OSError, ValueError):
            # OSError covers missing/permission/IsADirectory; ValueError covers
            # UnicodeDecodeError, which is not an OSError and would otherwise
            # escape and 500 the panel.
            continue
    return None


def _cpus() -> tuple[float | None, bool]:
    """(cpus, readable). None with readable True means genuinely unlimited;
    readable False means we could not tell. See the module docstring."""
    raw = _read_first(_CGROUP / "cpu.max")                          # cgroup v2
    if raw is not None:
        parts = raw.split()
        if parts and parts[0] == "max":
            return None, True
        if len(parts) == 2:
            try:
                quota, period = int(parts[0]), int(parts[1])
            except ValueError:
                return None, False
            if quota > 0 and period > 0:
                return round(quota / period, 3), True
        return None, False
    quota_raw = _read_first(_CGROUP / "cpu" / "cpu.cfs_quota_us")    # cgroup v1
    period_raw = _read_first(_CGROUP / "cpu" / "cpu.cfs_period_us")
    if quota_raw is None or period_raw is None:
        return None, False
    try:
        quota, period = int(quota_raw), int(period_raw)
    except ValueError:
        return None, False
    if quota <= 0:                      # -1 is v1's spelling of "unlimited"
        return None, True
    if period <= 0:
        return None, False
    return round(quota / period, 3), True


def _mem() -> tuple[int | None, bool]:
    """(bytes, readable), with its OWN readable flag — see the module docstring
    on why this must not borrow the CPU reader's."""
    raw = _read_first(_CGROUP / "memory.max",
                      _CGROUP / "memory" / "memory.limit_in_bytes")
    if raw is None:
        return None, False
    if raw == "max":
        return None, True
    try:
        value = int(raw)
    except ValueError:
        return None, False
    if value <= 0:
        return None, False
    # The v1 sentinel is "unlimited", and it IS a readable answer.
    return (None, True) if value >= _V1_UNLIMITED else (value, True)


def describe() -> dict[str, Any]:
    """Everything the panel needs about the container's own ceilings."""
    cpus, cpus_readable = _cpus()
    mem_bytes, mem_readable = _mem()
    return {
        "cpus": cpus,                     # None + readable => no limit at all
        "cpus_readable": cpus_readable,
        "mem_bytes": mem_bytes,
        "mem_readable": mem_readable,
        # The ceiling any limit sits under, and the number that makes a limit
        # meaningful or absurd: `--cpus=3` on a 2-core box is not a raise.
        "host_cpus": os.cpu_count(),
        # `docker update` takes an id, and the id is the container's hostname.
        # The NAME is not knowable from inside, so the id is what the panel can
        # honestly put in a command it asks someone to paste. Truncated to the
        # 12 chars docker itself displays, and stripped, because this string
        # ends up on a root shell's command line.
        #
        # ⚠️ Only meaningful when a cgroup was readable at all: on a bare Linux
        # host `/etc/hostname` is the machine name, and printing
        # `docker update --cpus=2 mydevbox` would be a confident command that
        # targets nothing. The panel gates on `cpus_readable` for that reason.
        "container_id": (_read_first(_HOSTNAME) or "")[:12],
    }
