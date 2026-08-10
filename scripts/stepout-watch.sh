#!/usr/bin/env bash
# stepout-watch.sh — read the step-out counters out of prod and say what they MEAN.
#
# The step-out shipped 2026-08-09 server-default-ON across the roster as a data run,
# and the raw numbers are easy to misread. Two of the three failure modes look like
# success: a step-out that ends the instant it starts still increments `stepouts`, and
# a step-out the one-hop deferral cap eats never increments it at all. The RATIO is
# the signal, not the count — so this prints the ratio and the reading, not a dump.
#
# Read-only: goes through scripts/prod-read.sh, which opens the DB `mode=ro`
# in-container. Nothing here can write.
#
# Usage:  scripts/stepout-watch.sh          # last 24h
#         scripts/stepout-watch.sh 72       # last 72h
#         scripts/stepout-watch.sh 6 --by-account
set -euo pipefail

cd "$(dirname "$0")/.."

HOURS="${1:-24}"
[[ "$HOURS" =~ ^[0-9]+$ ]] || { echo "usage: $0 [hours] [--by-account]" >&2; exit 2; }
BY_ACCOUNT=0
[[ "${2:-}" == "--by-account" ]] && BY_ACCOUNT=1

SQL="
SELECT COALESCE(SUM(json_extract(stats_json,'\$.stepouts')),0)        AS stepouts,
       COALESCE(SUM(json_extract(stats_json,'\$.stepout_broken')),0)  AS broke,
       COALESCE(SUM(json_extract(stats_json,'\$.stepout_blocked')),0) AS blocked,
       COALESCE(SUM(json_extract(stats_json,'\$.replies_sent')),0)    AS replies,
       COUNT(*) AS runs
FROM automation_runs
WHERE kind='ai_chatter' AND started_at > datetime('now','-${HOURS} hours');
"

printf '\n\033[1;36m▸ step-out counters — last %sh\033[0m\n' "$HOURS"
ROW=$(scripts/prod-read.sh "$SQL" | tail -1)
IFS='|' read -r STEPOUTS BROKE BLOCKED REPLIES RUNS <<<"${ROW// /}"

printf '  runs %s   replies %s   stepouts %s   broken %s   blocked %s\n\n' \
  "$RUNS" "$REPLIES" "$STEPOUTS" "$BROKE" "$BLOCKED"

if [[ "${STEPOUTS:-0}" -eq 0 ]]; then
  printf '  \033[1;33mnothing fired in this window.\033[0m Either the roster is quiet, or\n'
  printf '  `rhythm_stepout_enabled` is off, or every candidate is hot / just-sold /\n'
  printf '  inside its opening window. Widen the window before concluding anything.\n\n'
  exit 0
fi

pct() { awk -v a="$1" -v b="$2" 'BEGIN{ printf "%.0f", (b>0 ? 100*a/b : 0) }'; }
BROKE_PCT=$(pct "$BROKE" "$STEPOUTS")
BLOCK_PCT=$(pct "$BLOCKED" "$((STEPOUTS + BLOCKED))")

# ⚠️ MINIMUM SAMPLE. Without this the script confidently mis-reads its own noise:
# on 2026-08-10 it called 0-broken-of-3 "the gap filter is too strict" when the same
# measure had read a healthy 14% on 7 the day before. A ratio over a handful of
# events is not a signal, and a tool that shouts a verdict at n=3 is worse than one
# that says nothing — you act on it.
MIN_N=10
if [[ "$STEPOUTS" -lt "$MIN_N" ]]; then
  printf '  \033[1;33monly %s step-outs — too few to read a ratio.\033[0m\n' "$STEPOUTS"
  printf '  (broken %s, blocked %s. Need ~%s before the percentages mean anything —\n' \
    "$BROKE" "$BLOCKED" "$MIN_N"
  printf '   re-run over a wider window, e.g. %s 72.)\n\n' "$0"
  exit 0
fi

printf '  broken/stepouts  %3s%%  ' "$BROKE_PCT"
if   [[ "$BROKE_PCT" -gt 60 ]]; then
  printf '\033[1;31mTOO HIGH\033[0m — the exit is firing on messages he sent\n'
  printf '                          BEFORE she left, not on him double-texting.\n'
  printf '                          Suspect the `since` filter in _stepout.persisted.\n'
elif [[ "$BROKE_PCT" -lt 10 ]]; then
  printf '\033[1;33mTOO LOW\033[0m — the 60s gap filter is likely too strict;\n'
  printf '                          real double-texts are being read as one burst.\n'
else
  printf '\033[1;32mhealthy\033[0m — he double-texts her back roughly this often.\n'
fi

printf '  blocked share    %3s%%  ' "$BLOCK_PCT"
if [[ "$BLOCK_PCT" -gt 50 ]]; then
  printf '\033[1;31mTHE CAP IS EATING IT\033[0m — the one-hop `deferrals`\n'
  printf '                          guard is refusing more step-outs than it allows.\n'
  printf '                          The data run is not measuring what it claims to.\n'
else
  printf '\033[1;32mfine\033[0m — the one-hop cap is not dominating.\n'
fi
printf '\n'

if [[ "$BY_ACCOUNT" == "1" ]]; then
  printf '\033[1;36m▸ by account\033[0m\n'
  scripts/prod-read.sh "
    SELECT account_id,
           SUM(json_extract(stats_json,'\$.stepouts'))        AS stepouts,
           SUM(json_extract(stats_json,'\$.stepout_broken'))  AS broke,
           SUM(json_extract(stats_json,'\$.stepout_blocked')) AS blocked
    FROM automation_runs
    WHERE kind='ai_chatter' AND started_at > datetime('now','-${HOURS} hours')
    GROUP BY account_id
    HAVING stepouts > 0 OR blocked > 0
    ORDER BY stepouts DESC;
  "
  printf '\n'
fi
