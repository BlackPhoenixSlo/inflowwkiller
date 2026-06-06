#!/usr/bin/env bash
# Reload BOTH dev servers — the FastAPI relay (uvicorn :8787) AND the Next
# dev server (:3001) — without tearing down their supervisors.
#
# Why this exists: the relay runs WITHOUT uvicorn --reload (the supervisor
# wraps a plain `uvicorn service.server:app`), so edits to server.py /
# of_client.py / any service/*.py are NOT picked up until the uvicorn child
# is restarted. Next hot-reloads most code on its own, but next.config.ts
# changes (and wedged fast-refresh) need a real restart too.
#
# How it works: both supervisors (uvicorn-supervisor.sh / next-supervisor.sh)
# run a `while true` loop that relaunches their child the instant it dies. So
# we just kill the PORT LISTENER (the child) — NOT the supervisor — and the
# supervisor reaps + relaunches it in ~2s with the new code.
#
# Usage:
#   ./scripts/reload.sh              # bounce relay + next   (default)
#   ./scripts/reload.sh --relay      # bounce ONLY the relay (:8787)
#   ./scripts/reload.sh --next       # bounce ONLY next      (:3001)
#   ./scripts/reload.sh --no-wait    # don't wait for them to come back
#
# Run ./scripts/start.sh first if the supervisors aren't up yet.

set -u
cd "$(dirname "$0")/.."

HOST="${HOST:-127.0.0.1}"
RELAY_PORT="${PORT:-8787}"
NEXT_PORT="${NEXT_PORT:-3001}"

do_relay=true
do_next=true
do_wait=true
for arg in "$@"; do
  case "$arg" in
    --relay|--relay-only) do_next=false ;;
    --next|--next-only)   do_relay=false ;;
    --no-wait)            do_wait=false ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "[reload] unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# Kill whatever is listening on $1 (a port). SIGTERM first so uvicorn
# checkpoints SQLite (WAL → main) and drains in-flight requests; escalate to
# SIGKILL only if it's still bound after 6s. The supervisor relaunches it.
bounce() {
  local port="$1" label="$2"
  local pids
  pids="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
  if [[ -z "$pids" ]]; then
    echo "[reload] nothing on :$port ($label) — supervisor down? run ./scripts/start.sh"
    return 1
  fi
  echo "[reload] bouncing $label on :$port (pids: $pids) — supervisor will relaunch…"
  echo "$pids" | xargs kill 2>/dev/null || true
  for _ in 1 2 3 4 5 6; do
    sleep 1
    lsof -ti tcp:"$port" >/dev/null 2>&1 || return 0
  done
  echo "[reload] :$port still bound after 6s — force-killing."
  lsof -ti tcp:"$port" | xargs kill -9 2>/dev/null || true
  return 0
}

# Poll a URL until it answers (the supervisor's relaunch + boot time).
wait_up() {
  local url="$1" label="$2" tries="${3:-30}"
  for i in $(seq 1 "$tries"); do
    if curl -fsS -o /dev/null --max-time 5 "$url" 2>/dev/null; then
      echo "[reload] $label is back ($url)"
      return 0
    fi
    sleep 1
  done
  echo "[reload] timed out waiting for $label ($url) — check ./scripts/tail.sh"
  return 1
}

[[ "$do_relay" == "true" ]] && bounce "$RELAY_PORT" "relay/uvicorn" || true
[[ "$do_next"  == "true" ]] && bounce "$NEXT_PORT"  "next dev"      || true

if [[ "$do_wait" == "true" ]]; then
  echo "[reload] waiting for services to come back…"
  [[ "$do_relay" == "true" ]] && wait_up "http://${HOST}:${RELAY_PORT}/health" "relay" 30 || true
  # Next's first compile after restart can be slow — give it longer.
  [[ "$do_next" == "true" ]] && wait_up "http://${HOST}:${NEXT_PORT}/" "next" 60 || true
fi

echo "[reload] done."
