#!/usr/bin/env bash
# Live tail of all relay + ui logs, color-coded by source. Use this as
# your default "what's happening right now" view. Optional filters for
# fast triage.
#
# Usage:
#   ./scripts/tail.sh           # everything from all 3 logs, live
#   ./scripts/tail.sh -e        # errors / warnings / tracebacks only
#   ./scripts/tail.sh -s        # slow API calls only (>2s)
#   ./scripts/tail.sh -i        # /img calls only (the bulk of the noise)
#   ./scripts/tail.sh -n 200    # static snapshot, last N lines instead of live
#
# Stop with Ctrl-C.

set -u

MODE="all"
LINES=200
LIVE=true
while [[ $# -gt 0 ]]; do
  case "$1" in
    -e) MODE="errors" ;;
    -s) MODE="slow" ;;
    -i) MODE="img" ;;
    -n) shift; LINES="$1"; LIVE=false ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^#//; s/^ //'
      exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
  shift
done

if [[ -t 1 ]]; then
  RELAY_C=$'\033[36m'; NEXT_C=$'\033[35m'; API_C=$'\033[33m'
  R=$'\033[31m'; G=$'\033[32m'; N=$'\033[0m'
else
  RELAY_C=""; NEXT_C=""; API_C=""; R=""; G=""; N=""
fi

# Common filter for "errors" mode. /img request-spam lines that 200 are
# never interesting; explicitly drop them so the signal isn't drowned.
errors_filter='ERROR|Traceback|Exception|RangeError|EADDRINUSE|ELIFECYCLE|WARNING|warn:|timeout|upstream_(timeout|network|unreachable)|503 |502 |500 |499 |429 |404 '

# Slow filter for /tmp/of-api.log. The log line shape is:
#   2026-05-20T16:30:00.000Z GET    200  3275ms https://…
# So column $4 is the duration with "ms" suffix.
slow_awk='/[0-9]+ms / { gsub(/ms/,"",$4); if ($4+0 > 2000) print }'

prefix_relay() { sed -u "s|^|${RELAY_C}[relay]${N} |"; }
prefix_next()  { sed -u "s|^|${NEXT_C}[next ]${N} |"; }
prefix_api()   { sed -u "s|^|${API_C}[api  ]${N} |"; }

run() {
  local LOG="$1"; local PREFIX_FN="$2"; local EXTRA_FILTER="$3"
  [[ ! -f "$LOG" ]] && return
  if $LIVE; then
    if [[ -n "$EXTRA_FILTER" ]]; then
      tail -n 0 -F "$LOG" 2>/dev/null | grep -E --line-buffered "$EXTRA_FILTER" | $PREFIX_FN &
    else
      tail -n 0 -F "$LOG" 2>/dev/null | $PREFIX_FN &
    fi
  else
    if [[ -n "$EXTRA_FILTER" ]]; then
      tail -n "$LINES" "$LOG" 2>/dev/null | grep -E "$EXTRA_FILTER" | $PREFIX_FN
    else
      tail -n "$LINES" "$LOG" 2>/dev/null | $PREFIX_FN
    fi
  fi
}

# Make sure background tails die when this script does.
trap 'kill $(jobs -p) 2>/dev/null' EXIT

case "$MODE" in
  errors)
    run /tmp/of-relay.log prefix_relay "$errors_filter"
    run /tmp/of-inbox.log prefix_next  "$errors_filter"
    run /tmp/of-api.log   prefix_api   "$errors_filter"
    ;;
  slow)
    # /tmp/of-api.log is the only one with timing data; -F + awk in
    # streaming mode works fine.
    if $LIVE; then
      tail -n 0 -F /tmp/of-api.log 2>/dev/null | awk "$slow_awk" | prefix_api &
    else
      tail -n "$LINES" /tmp/of-api.log 2>/dev/null | awk "$slow_awk" | prefix_api
    fi
    ;;
  img)
    run /tmp/of-api.log   prefix_api   '/img\?|/img/by-hash'
    ;;
  all)
    run /tmp/of-relay.log prefix_relay ""
    run /tmp/of-inbox.log prefix_next  ""
    run /tmp/of-api.log   prefix_api   ""
    ;;
esac

$LIVE && wait
