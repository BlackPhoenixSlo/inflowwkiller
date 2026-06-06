#!/usr/bin/env bash
# One-shot health + failure triage. Run this FIRST when something is off.
#
# Prints everything you need to triage in under 2 seconds:
#   - process tree + ports
#   - health probes (relay + UI)
#   - supervisor restart history (caught a Turbopack crash overnight? you'll see)
#   - last errors + warnings from each log
#   - slow API calls in the last hour
#   - recent abnormal repeat-request rates (the "popout loop" signal)
#   - image-proxy live stats
#
# Usage:
#   ./scripts/diag.sh           # human-readable
#   ./scripts/diag.sh -q        # quiet — only failures + anomalies

set -u
cd "$(dirname "$0")/.."

QUIET=false
[[ "${1:-}" == "-q" ]] && QUIET=true

# Color codes — bash with no TTY (cron) doesn't get them.
if [[ -t 1 ]]; then
  R=$'\033[31m'; Y=$'\033[33m'; G=$'\033[32m'; B=$'\033[34m'; D=$'\033[2m'; N=$'\033[0m'
else
  R=""; Y=""; G=""; B=""; D=""; N=""
fi

ok()   { echo "  ${G}✓${N} $1"; }
warn() { echo "  ${Y}⚠${N} $1"; }
bad()  { echo "  ${R}✗${N} $1"; }
section() { $QUIET || echo; $QUIET || echo "${B}━━━ $1 ━━━${N}"; }

problems=0
inc() { problems=$((problems + 1)); }

# ── 1. supervisors + children ─────────────────────────────────
# Parallel arrays because Apple's bash 3.2 has no associative arrays.
section "processes"
PID_FILES=(/tmp/of-relay.pid /tmp/of-inbox.pid /tmp/of-rotator.pid)
PID_NAMES=(relay-supervisor next-supervisor log-rotator)
for i in 0 1 2; do
  f="${PID_FILES[$i]}"
  name="${PID_NAMES[$i]}"
  if [[ ! -f "$f" ]]; then bad "$name: PID file missing"; inc; continue; fi
  PID=$(cat "$f")
  if ! kill -0 "$PID" 2>/dev/null; then bad "$name: PID $PID dead"; inc; continue; fi
  CHILDREN=$(pgrep -P "$PID" 2>/dev/null | tr '\n' ' ')
  $QUIET || ok "$name (pid $PID) children: ${CHILDREN:-none}"
done

# ── 2. ports ──────────────────────────────────────────────────
section "ports"
for PORT in 8787 3001; do
  LISTENER=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | tail -n +2 | head -1)
  if [[ -z "$LISTENER" ]]; then bad "port :$PORT NOT listening"; inc
  else
    PID=$(echo "$LISTENER" | awk '{print $2}')
    CMD=$(echo "$LISTENER" | awk '{print $1}')
    $QUIET || ok "port :$PORT ← $CMD (pid $PID)"
  fi
done

# ── 3. health probes ──────────────────────────────────────────
section "health"
TOK="${SHARE_TOKEN:-}"
for url in \
  "http://127.0.0.1:8787/health" \
  "http://127.0.0.1:3001/inbox?t=$TOK" \
  "http://127.0.0.1:8787/admin/img-cache/stats?t=$TOK" \
; do
  short="${url#http://127.0.0.1:}"
  short="${short%%\?*}"
  resp=$(curl -sS -o /dev/null -w "%{http_code} %{time_total}" --max-time 8 "$url" 2>&1 || echo "000 timeout")
  code="${resp%% *}"; tm="${resp##* }"
  if [[ "$code" == "200" ]]; then $QUIET || ok "$short → 200 in ${tm}s"
  else bad "$short → $code in ${tm}s"; inc
  fi
done

# ── 4. supervisor restart history ─────────────────────────────
section "supervisor restarts (last 24h)"
RELAY_R=$(grep -c "\[relay-sup\] launching" /tmp/of-relay.log 2>/dev/null || echo 0)
NEXT_R=$(grep -c "\[next-sup\] launching" /tmp/of-inbox.log 2>/dev/null || echo 0)
[[ "$RELAY_R" -gt 5 ]] && warn "relay supervisor relaunched $RELAY_R times — possible flapping" && inc
[[ "$NEXT_R" -gt 5 ]] && warn "next supervisor relaunched $NEXT_R times — possible flapping" && inc
$QUIET || ok "relay-sup launches: $RELAY_R   next-sup launches: $NEXT_R"

# ── 5. last errors / exceptions ────────────────────────────────
section "errors (last 5 per service)"
for f in /tmp/of-relay.log /tmp/of-inbox.log; do
  [[ ! -f $f ]] && continue
  hits=$(grep -aE "ERROR|Traceback|Exception|RangeError|EADDRINUSE|ELIFECYCLE" "$f" 2>/dev/null | tail -5)
  if [[ -n "$hits" ]]; then
    warn "${f}:"
    echo "$hits" | sed "s/^/      ${D}/" | sed "s/$/${N}/"
    inc
  else
    $QUIET || ok "$f: no errors"
  fi
done

# ── 6. slow API calls (last 30 min, >2s) ──────────────────────
section "slow API calls (>2s, last 30 min)"
if [[ -f /tmp/of-api.log ]]; then
  CUTOFF=$(date -v -30M +"%Y-%m-%dT%H:%M" 2>/dev/null || date -d '30 min ago' +"%Y-%m-%dT%H:%M" 2>/dev/null || echo "")
  if [[ -n "$CUTOFF" ]]; then
    slow=$(awk -v cutoff="$CUTOFF" '$1>=cutoff && $4 ~ /ms/ { gsub(/ms/,"",$4); if ($4 > 2000) print $1, $4"ms", $5 }' /tmp/of-api.log 2>/dev/null | tail -10)
    if [[ -n "$slow" ]]; then
      warn "slow calls:"
      echo "$slow" | sed "s/^/      ${D}/" | sed "s/$/${N}/"
    else
      $QUIET || ok "no slow calls"
    fi
  fi
fi

# ── 7. repeat-request anomalies (popout-loop signal) ───────────
section "repeat-request rates"
if [[ -f /tmp/of-inbox.log ]]; then
  # Strip timing, find any URL hit > 50 times in the recent tail
  REPEATS=$(tail -2000 /tmp/of-inbox.log 2>/dev/null \
    | awk '/^ GET / { print $2 }' \
    | sort | uniq -c | sort -rn | awk '$1>50 { print }' | head -5)
  if [[ -n "$REPEATS" ]]; then
    warn "URLs hit >50 times in last 2000 log lines (possible loop):"
    echo "$REPEATS" | sed "s/^/      ${D}/" | sed "s/$/${N}/"
    inc
  else
    $QUIET || ok "no abnormal repeat rates"
  fi
fi

# ── 8. log sizes (rotator working?) ───────────────────────────
section "log sizes"
for f in /tmp/of-relay.log /tmp/of-inbox.log /tmp/of-api.log; do
  [[ ! -f $f ]] && continue
  size=$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null || echo 0)
  MiB=$(( size / 1048576 ))
  if [[ "$MiB" -gt 40 ]]; then warn "$f: ${MiB} MiB — rotator should fire next sweep"
  else $QUIET || ok "$f: $(echo "scale=1; $size / 1024" | bc) KiB"
  fi
done

# ── 9. image cache live stats ─────────────────────────────────
section "image cache"
stats=$(curl -sS --max-time 3 "http://127.0.0.1:8787/admin/img-cache/stats?t=$TOK" 2>/dev/null)
if [[ -n "$stats" ]]; then
  $QUIET || echo "  $stats"
fi

# ── verdict ────────────────────────────────────────────────────
echo
if [[ "$problems" -eq 0 ]]; then
  echo "${G}═══ ALL CLEAR ═══${N}"
  exit 0
else
  echo "${R}═══ ${problems} PROBLEM(S) ═══${N}"
  echo "  Next steps:"
  echo "    ./scripts/tail.sh           # live combined logs"
  echo "    ./scripts/tail.sh -e        # errors only"
  echo "    ./scripts/stop.sh && ./scripts/start.sh   # nuclear restart"
  exit 1
fi
