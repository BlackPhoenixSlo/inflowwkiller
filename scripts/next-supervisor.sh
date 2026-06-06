#!/usr/bin/env bash
# Wraps `pnpm dev` so Next self-heals when Turbopack's AsyncHook tracker
# overflows V8's internal Map size limit (RangeError: Map maximum size
# exceeded) after many hours of dev traffic. Without this, the process
# dies and /inbox returns 502 until someone manually restarts.
#
# Backoff: a healthy run that lasted >60s resets the counter and we
# restart in 2s. Fast-failure runs (port already taken, syntax error in
# next.config.ts, missing deps) back off exponentially up to 30s so we
# don't hot-loop and burn CPU.
#
# Cleanup: stop.sh sends SIGTERM to this supervisor's PID. The trap
# forwards it to the child `pnpm dev` and waits for it to actually die
# before exiting, so port 3001 is free for the next start.

set -u
cd "$(dirname "$0")/.."

CHILD_PID=""

cleanup() {
  echo "[next-sup] received signal — shutting down child $CHILD_PID"
  if [[ -n "$CHILD_PID" ]] && kill -0 "$CHILD_PID" 2>/dev/null; then
    kill -TERM "$CHILD_PID" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8; do
      kill -0 "$CHILD_PID" 2>/dev/null || break
      sleep 1
    done
    kill -9 "$CHILD_PID" 2>/dev/null || true
  fi
  # Belt-and-suspenders: pnpm spawns node under itself; if pnpm exited
  # but node is still bound to :3001, the next start.sh will refuse to
  # boot. Reap anything still on the port.
  if command -v lsof >/dev/null 2>&1; then
    PORT_PIDS="$(lsof -ti :3001 2>/dev/null || true)"
    [[ -n "$PORT_PIDS" ]] && echo "$PORT_PIDS" | xargs -I{} kill -9 {} 2>/dev/null || true
  fi
  exit 0
}
trap cleanup TERM INT

ATTEMPT=0
while true; do
  ATTEMPT=$((ATTEMPT + 1))
  # Reap any node still bound to :3001 before relaunching. In the real
  # Turbopack RangeError crash, pnpm + node die together. But if only
  # pnpm dies (e.g. OOM picks the parent), its node child can survive
  # and hold the port — next `pnpm dev` would hit EADDRINUSE and loop
  # forever. Be defensive.
  if command -v lsof >/dev/null 2>&1; then
    PORT_PIDS="$(lsof -ti :3001 2>/dev/null || true)"
    if [[ -n "$PORT_PIDS" ]]; then
      echo "[next-sup] reaping leftover :3001 listener(s): $PORT_PIDS"
      echo "$PORT_PIDS" | xargs -I{} kill -9 {} 2>/dev/null || true
      sleep 1
    fi
  fi
  echo "[next-sup] launching pnpm dev (attempt $ATTEMPT) at $(date '+%Y-%m-%d %H:%M:%S')"
  START=$(date +%s)
  ( cd app && exec pnpm dev ) &
  CHILD_PID=$!
  wait "$CHILD_PID"
  CODE=$?
  END=$(date +%s)
  RUNTIME=$((END - START))

  if [[ "$RUNTIME" -gt 60 ]]; then
    # Last run was healthy — this was a crash after real uptime, not a
    # boot failure. Reset backoff so the restart is snappy.
    ATTEMPT=0
    SLEEP=2
  else
    SLEEP=$(( 2 ** ATTEMPT ))
    [[ "$SLEEP" -gt 30 ]] && SLEEP=30
  fi
  echo "[next-sup] pnpm dev exited code=$CODE after ${RUNTIME}s — restart in ${SLEEP}s"
  sleep "$SLEEP"
done
