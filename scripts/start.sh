#!/usr/bin/env bash
# Start the OF Relay locally (host uvicorn — not Docker) and, if available,
# expose it via Tailscale Funnel so you can reach the UI from any device.
#
# Idempotent: if a relay is already running on $PORT, we reuse it rather
# than crash on "Address already in use".
#
# Files written:
#   /tmp/of-relay.pid   PID of the uvicorn process (used by stop.sh)
#   /tmp/of-relay.log   uvicorn stdout + stderr
#
# Usage:
#   ./scripts/start.sh              # uvicorn + tailscale funnel
#   ./scripts/start.sh --no-tunnel  # uvicorn only
#   PORT=9000 ./scripts/start.sh    # override port

set -euo pipefail

cd "$(dirname "$0")/.."        # repo root regardless of CWD
PORT="${PORT:-8787}"
HOST="${HOST:-127.0.0.1}"
PIDFILE="/tmp/of-relay.pid"
LOGFILE="/tmp/of-relay.log"
VENV="venv/bin/uvicorn"
# Next.js dev server hosts the /inbox UI and proxies /api/* + /ui/* to
# the relay. Funnel points here so /inbox is the public default.
NEXT_PORT="${NEXT_PORT:-3001}"
NEXT_PIDFILE="/tmp/of-inbox.pid"
NEXT_LOGFILE="/tmp/of-inbox.log"

if [[ ! -x "$VENV" ]]; then
  echo "[start] $VENV not found — create the venv first:"
  echo "         python3 -m venv venv && venv/bin/pip install -r requirements.txt"
  exit 1
fi

want_tunnel=true
for arg in "$@"; do
  case "$arg" in
    --no-tunnel) want_tunnel=false ;;
    *) echo "[start] unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# ─── uvicorn ────────────────────────────────────────────────────
if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null \
   && curl -fsS -o /dev/null "http://${HOST}:${PORT}/health" 2>/dev/null; then
  echo "[start] relay already running (pid $(cat "$PIDFILE"))"
else
  # Stale PID file or stale process — clean up before relaunch.
  if [[ -f "$PIDFILE" ]]; then
    OLD=$(cat "$PIDFILE")
    kill -0 "$OLD" 2>/dev/null && kill "$OLD" || true
    rm -f "$PIDFILE"
  fi
  # macOS holds the port briefly in TIME_WAIT after a kill — wait it out.
  for _ in 1 2 3 4 5; do
    if ! lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then break; fi
    sleep 1
  done

  echo "[start] launching uvicorn supervisor on ${HOST}:${PORT} (logs: $LOGFILE)"
  # Supervisor wraps uvicorn in a self-healing loop. RELOAD=1 keeps the
  # legacy --reload path (no supervisor: watchfiles already restarts the
  # worker on file change, so wrapping it would create a recursion).
  if [[ "${RELOAD:-0}" == "1" ]]; then
    echo "[start] --reload enabled (RELOAD=1). Expect periodic 10–15s freezes when chat traffic is high."
    echo "[start] WARNING: --reload bypasses the supervisor; a crash here will not self-heal."
    nohup "$VENV" service.server:app --host "$HOST" --port "$PORT" --reload \
      --reload-exclude '*.db' \
      --reload-exclude '*.db-*' \
      --reload-exclude '*.pyc' \
      --reload-exclude '__pycache__/*' \
      > "$LOGFILE" 2>&1 &
  else
    PORT="$PORT" HOST="$HOST" \
      nohup ./scripts/uvicorn-supervisor.sh > "$LOGFILE" 2>&1 &
  fi
  echo $! > "$PIDFILE"
  disown 2>/dev/null || true

  # Wait up to 20s for /health to answer. The PID-alive check that was
  # here is gone: with the supervisor, an early uvicorn crash gets
  # auto-restarted, so the supervisor PID staying alive doesn't mean the
  # relay is ready. /health is the only thing that answers that.
  for i in $(seq 1 20); do
    if curl -fsS -o /dev/null "http://${HOST}:${PORT}/health" 2>/dev/null; then
      echo "[start] relay ready (supervisor pid $(cat "$PIDFILE"))"
      break
    fi
    sleep 1
    if [[ $i -eq 20 ]]; then
      echo "[start] timed out waiting for /health — tail of $LOGFILE:"
      tail -20 "$LOGFILE"
    fi
  done
fi

# ─── next.js dev (serves /inbox; proxies /ui, /api/*, /admin/*, /events) ───
if [[ -f "$NEXT_PIDFILE" ]] && kill -0 "$(cat "$NEXT_PIDFILE")" 2>/dev/null \
   && curl -fsS -o /dev/null "http://${HOST}:${NEXT_PORT}/" 2>/dev/null; then
  echo "[start] next dev already running (pid $(cat "$NEXT_PIDFILE"))"
elif [[ -d app && -f app/package.json ]] && command -v pnpm >/dev/null 2>&1; then
  if [[ -f "$NEXT_PIDFILE" ]]; then
    OLD=$(cat "$NEXT_PIDFILE")
    kill -0 "$OLD" 2>/dev/null && kill "$OLD" || true
    rm -f "$NEXT_PIDFILE"
  fi
  echo "[start] launching next dev supervisor on ${HOST}:${NEXT_PORT} (logs: $NEXT_LOGFILE)"
  # Supervisor wraps pnpm dev in a self-healing loop; without it, the
  # Turbopack AsyncHook leak (RangeError: Map maximum size exceeded)
  # leaves /inbox 502'd until someone restarts manually.
  nohup ./scripts/next-supervisor.sh > "$NEXT_LOGFILE" 2>&1 &
  echo $! > "$NEXT_PIDFILE"
  disown 2>/dev/null || true
  for i in $(seq 1 30); do
    if curl -fsS -o /dev/null "http://${HOST}:${NEXT_PORT}/" 2>/dev/null; then
      echo "[start] next ready (supervisor pid $(cat "$NEXT_PIDFILE"))"
      # Pre-warm Turbopack's /inbox compile so the first human hit doesn't
      # eat the cold-compile delay. Backgrounded — start.sh returns now,
      # warm-up happens in parallel. Retry a few times in case dev is
      # still wiring up its handlers in the first second.
      WARM_TOKEN="${SHARE_TOKEN:-}"
      (
        for _ in 1 2 3 4 5; do
          curl -fsS -o /dev/null --max-time 90 \
            "http://${HOST}:${NEXT_PORT}/inbox?t=${WARM_TOKEN}" 2>/dev/null && break
          sleep 1
        done
      ) >/dev/null 2>&1 &
      disown 2>/dev/null || true
      break
    fi
    sleep 1
    [[ $i -eq 30 ]] && echo "[start] timed out waiting for next dev — tail of $NEXT_LOGFILE:" && tail -10 "$NEXT_LOGFILE"
  done
else
  echo "[start] skipping next dev (app/ or pnpm missing) — funnel will expose /ui only"
fi

# ─── log rotator ────────────────────────────────────────────────
# Hourly rotate. Idempotent and cheap; only rewrites a file once it's
# over the size cap (50 MiB by default — see scripts/rotate-logs.sh).
# Without this, /tmp/of-relay.log and /tmp/of-api.log grow ~50-200 MB/wk
# at INFO-level OF traffic and eventually fill /tmp.
ROTATOR_PIDFILE="/tmp/of-rotator.pid"
if [[ -f "$ROTATOR_PIDFILE" ]] && kill -0 "$(cat "$ROTATOR_PIDFILE")" 2>/dev/null; then
  echo "[start] log rotator already running (pid $(cat "$ROTATOR_PIDFILE"))"
else
  rm -f "$ROTATOR_PIDFILE"
  # Same loop also prunes the storyboard cache once per cycle. The prune
  # script is a no-op when nothing is stale, so running it hourly is fine
  # (it only acts on >7-day-old entries by default).
  nohup bash -c 'while true; do ./scripts/rotate-logs.sh >/dev/null 2>&1; ./scripts/prune-storyboards.sh >>/tmp/of-storyboard-prune.log 2>&1; sleep 3600; done' \
    > /dev/null 2>&1 &
  echo $! > "$ROTATOR_PIDFILE"
  disown 2>/dev/null || true
  echo "[start] log rotator + storyboard prune launched (pid $(cat "$ROTATOR_PIDFILE"), hourly)"
fi

# ─── tailscale funnel ───────────────────────────────────────────
# Funnel target precedence: if Next dev is up, point at it (so /inbox is
# the default route). Otherwise fall back to the relay's /ui port.
FUNNEL_TARGET="$PORT"
if [[ -f "$NEXT_PIDFILE" ]] && kill -0 "$(cat "$NEXT_PIDFILE")" 2>/dev/null; then
  FUNNEL_TARGET="$NEXT_PORT"
fi

if [[ "$want_tunnel" == "true" ]]; then
  if ! command -v tailscale >/dev/null 2>&1; then
    echo "[start] tailscale CLI not found — skipping funnel."
    echo "         Local /inbox: http://${HOST}:${NEXT_PORT}/inbox/"
    echo "         Local /ui:    http://${HOST}:${PORT}/ui/"
    exit 0
  fi
  echo "[start] enabling tailscale funnel → 127.0.0.1:${FUNNEL_TARGET}"
  tailscale funnel --bg "$FUNNEL_TARGET" || {
    echo "[start] funnel failed (not signed in / not enabled in admin?). "
    echo "         Local /inbox: http://${HOST}:${NEXT_PORT}/inbox/"
    echo "         Local /ui:    http://${HOST}:${PORT}/ui/"
    exit 0
  }
fi

# Show the share-token URL — the SHARE_TOKEN constant in service/server.py
# is the stable default; users overriding via env see the new value
# instantiated when uvicorn read the env, not here.
# Append ?t=... ONLY when a share token is actually set in the env. The
# gate is disabled by default (SHARE_TOKEN="" exported in
# scripts/uvicorn-supervisor.sh), so the printed URLs stay clean and no
# token is ever needed. Set SHARE_TOKEN=<token> to re-enable both the gate
# and this suffix.
if [[ -n "${SHARE_TOKEN:-}" ]]; then QS="?t=${SHARE_TOKEN}"; else QS=""; fi

# Detect whether the optional Docker capture sidecar is reachable so we can
# print "remote login enabled" / "remote login disabled" alongside the URLs.
# The sidecar binds 127.0.0.1:6080+6081 when `docker compose --profile capture
# up -d capture` is running.
cap_msg="(no capture sidecar — only local 'playwright-proxy' mode works)"
if curl -fsS -o /dev/null --max-time 1 "http://127.0.0.1:6081/health" 2>/dev/null; then
  cap_msg="(✓ capture sidecar reachable — 🌐 Login on the server works from any device)"
fi

echo
echo "  Local /inbox: http://${HOST}:${NEXT_PORT}/inbox${QS}"
echo "  Local /ui:    http://${HOST}:${PORT}/ui/${QS}"
if [[ "$want_tunnel" == "true" ]] && command -v tailscale >/dev/null 2>&1; then
  TS_HOST="$(tailscale status --json 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null || true)"
  if [[ -n "$TS_HOST" ]]; then
    echo "  Tailscale:    https://${TS_HOST}/inbox${QS}"
    echo "                https://${TS_HOST}/ui/${QS}"
  fi
fi
echo "  Capture:   ${cap_msg}"
echo "  Stop:      ./scripts/stop.sh"
