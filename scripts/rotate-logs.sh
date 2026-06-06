#!/usr/bin/env bash
# Rotate the relay + next dev log files when they cross a size cap.
# Single-generation keep (.1 only) — these are dev logs, not audit logs.
# Idempotent; safe to call from cron or a background loop.
#
# Why this exists: /tmp/of-relay.log grows ~50-200 MB/week at INFO with
# real OF traffic. /tmp is real disk on macOS, so unchecked it'll fill
# disk over months. Plus tailing a 500 MB log to debug is no fun.

set -u
MAX_BYTES="${MAX_BYTES:-52428800}"   # 50 MiB default
LOGS=(
  "/tmp/of-relay.log"
  "/tmp/of-inbox.log"
  "/tmp/of-api.log"
)

for f in "${LOGS[@]}"; do
  [[ -f "$f" ]] || continue
  # `stat -f%z` is BSD/macOS; `stat -c%s` is GNU. Probe once.
  if size=$(stat -f%z "$f" 2>/dev/null); then :; else size=$(stat -c%s "$f" 2>/dev/null || echo 0); fi
  if [[ "$size" -gt "$MAX_BYTES" ]]; then
    echo "[rotate] $f $size bytes > $MAX_BYTES — rotating"
    # `mv + open` is racy if the writer keeps a fd to the old inode;
    # both supervisors use append-mode open per-line / per-record so
    # losing the fd at mv-time loses at most one in-flight line. We
    # truncate the original instead so the writer's fd survives.
    cp "$f" "${f}.1" 2>/dev/null || true
    : > "$f"
  fi
done

# Loop's last command is the size test, which is false when the last
# log doesn't need rotation — leaking exit 1 confuses the launchd loop
# that reads $? for "is the rotator healthy?". Always exit 0.
exit 0
