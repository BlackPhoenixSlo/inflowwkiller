#!/usr/bin/env bash
# vps-watchdog.sh — the 4-hour blind spot, closed.
#
# On 2026-08-21 this box ran at 100% CPU from ~19:00 to ~22:48 and the FIRST
# thing to notice was Hostinger's throttle. By then the cap was applied and it
# outlived the bug that caused it by ~18 hours. Nothing was watching.
#
# Runs from cron every few minutes. Deliberately cheap — /proc reads and at
# most one HTTP call — because it has to work on a box that has no CPU left,
# which is precisely when it matters.
#
# STEAL WITHOUT SLEEPING: `top -bn1`'s CPU line is an average SINCE BOOT, and
# taking two samples means sleeping, which a cron job should not do. Instead we
# persist the raw /proc/stat counters and diff against the PREVIOUS RUN — the
# window is the cron interval, the cost is one file read.
#
# Alerts fire on STATE TRANSITIONS only (ok->bad, bad->ok), never on every
# tick. An alerter that repeats itself every 3 minutes gets muted by the human,
# and a muted alerter is worse than none.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
NOTIFY="$HERE/notify.sh"
STATE="${WATCHDOG_STATE:-/var/lib/fastt-watchdog}"
CFG="${WATCHDOG_CONFIG:-/root/fastt/watchdog.env}"
# shellcheck disable=SC1090
[ -f "$CFG" ] && . "$CFG"
mkdir -p "$STATE" 2>/dev/null || true

STEAL_WARN="${STEAL_WARN:-25}"
LOAD_MULT="${LOAD_MULT:-4}"
DISK_WARN="${DISK_WARN:-85}"
PUBLIC_URL="${PUBLIC_URL:-https://fastt.lol/}"
CONTAINERS="${WATCH_CONTAINERS-fastt-relay fastt-app}"

# transition <key> <bad:0|1> <severity> <title> <body>
# Emits only when the state CHANGES, and emits an explicit recovery so a
# silent channel is never ambiguous between "fine" and "watchdog died".
transition() {
  local key="$1" bad="$2" sev="$3" title="$4" body="${5:-}"
  local f="$STATE/$key" prev="ok"
  [ -f "$f" ] && prev="$(cat "$f" 2>/dev/null || echo ok)"
  if [ "$bad" -eq 1 ]; then
    if [ "$prev" != "bad" ]; then
      echo bad > "$f"; "$NOTIFY" "$sev" "$title" "$body" || true
    fi
  else
    if [ "$prev" = "bad" ]; then
      echo ok > "$f"; "$NOTIFY" ok "RECOVERED: $title" "" || true
    fi
    echo ok > "$f"
  fi
}

CORES="$(nproc 2>/dev/null || echo 1)"

# ── 1. CPU steal, diffed against the previous run ────────────────────────
PREV="$STATE/cpustat"
read -r _ u n s i w irq si st _ < /proc/stat
NOW_TOTAL=$((u+n+s+i+w+irq+si+st)); NOW_STEAL=$st
if [ -f "$PREV" ]; then
  read -r P_TOTAL P_STEAL < "$PREV"
  D_TOTAL=$((NOW_TOTAL-P_TOTAL)); D_STEAL=$((NOW_STEAL-P_STEAL))
  if [ "$D_TOTAL" -gt 0 ]; then
    STEAL_PCT=$(( D_STEAL * 100 / D_TOTAL ))
    if [ "$STEAL_PCT" -ge "$STEAL_WARN" ]; then
      transition steal 1 crit "CPU throttled by the host" \
        "steal=${STEAL_PCT}% over the last interval. The VM is ready to run and the hypervisor is refusing it — this is a provider cap, not app load. No code change clears it."
    else
      transition steal 0 ok "CPU throttled by the host"
    fi
  fi
fi
echo "$NOW_TOTAL $NOW_STEAL" > "$PREV"

# ── 2. Load average ──────────────────────────────────────────────────────
LOAD1="$(cut -d' ' -f1 /proc/loadavg)"
LOAD_INT="${LOAD1%%.*}"
if [ "$LOAD_INT" -ge $(( CORES * LOAD_MULT )) ]; then
  transition load 1 crit "Load average runaway" \
    "load1=${LOAD1} on ${CORES} cores (threshold ${LOAD_MULT}x). Something is spinning or everything is blocked."
else
  transition load 0 ok "Load average runaway"
fi

# ── 3. Containers up + healthy ───────────────────────────────────────────
BADC=""
for c in $CONTAINERS; do
  st="$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$c" 2>/dev/null || echo missing)"
  case "$st" in
    running/healthy|running/none) ;;
    *) BADC="$BADC $c=$st" ;;
  esac
done
if [ -n "$BADC" ]; then
  transition containers 1 crit "Container not healthy" "$BADC"
else
  transition containers 0 ok "Container not healthy"
fi

# ── 4. Public route ──────────────────────────────────────────────────────
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 "$PUBLIC_URL" 2>/dev/null)"
case "$CODE" in [0-9][0-9][0-9]) ;; *) CODE=000 ;; esac
if [ "$CODE" != "200" ]; then
  transition route 1 crit "Site not answering" "GET $PUBLIC_URL returned $CODE"
else
  transition route 0 ok "Site not answering"
fi

# ── 5. Disk ──────────────────────────────────────────────────────────────
DISK="$(df -P / | awk 'NR==2{gsub("%","",$5); print $5}')"
if [ "${DISK:-0}" -ge "$DISK_WARN" ]; then
  transition disk 1 warn "Disk filling" "root at ${DISK}% (threshold ${DISK_WARN}%)"
else
  transition disk 0 ok "Disk filling"
fi

exit 0
