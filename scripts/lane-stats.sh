#!/usr/bin/env bash
# Read the per-account OF priority lane and answer ONE question: is that lane
# actually a queue? Print it as a verdict, not a metrics dump.
#
# This exists because the observe-first half of P5 shipped as a raw admin JSON
# endpoint behind a sign-in — data nobody can read is not observability.
#
# Usage:
#   ./scripts/lane-stats.sh              # human summary + verdict
#   ./scripts/lane-stats.sh --json       # raw endpoint JSON, nothing else
#
# Environment:
#   RELAY       relay base URL          (default http://127.0.0.1:8797)
#   LANE_USER   username to sign in as  (prompted if unset)
#   LANE_PASS   password                (prompted if unset, never echoed)
#   SHARE_TOKEN appended as ?t= when the share-token gate is enabled
#
# Any signed-in user works — this route carries no account id, so it never
# reaches the per-account ownership check.
set -euo pipefail

RELAY="${RELAY:-http://127.0.0.1:8797}"
JSON_ONLY=false
[[ "${1:-}" == "--json" ]] && JSON_ONLY=true

JAR="$(mktemp -t lanestats)"
trap 'rm -f "$JAR"' EXIT
chmod 600 "$JAR"

URL="$RELAY/admin/priority-lanes/stats"
[[ -n "${SHARE_TOKEN:-}" ]] && URL="$URL?t=$SHARE_TOKEN"

fetch() { curl -sS --max-time 10 -b "$JAR" -o "$1" -w '%{http_code}' "$URL"; }

OUT="$(mktemp -t lanestats-body)"
trap 'rm -f "$JAR" "$OUT"' EXIT
CODE="$(fetch "$OUT")"

if [[ "$CODE" == "401" ]]; then
  USER_NAME="${LANE_USER:-}"
  PASS="${LANE_PASS:-}"
  if [[ -z "$USER_NAME" ]]; then
    [[ -t 0 ]] || { echo "relay needs a sign-in; set LANE_USER and LANE_PASS" >&2; exit 2; }
    read -r -p "username: " USER_NAME
  fi
  if [[ -z "$PASS" ]]; then
    [[ -t 0 ]] || { echo "relay needs a sign-in; set LANE_PASS" >&2; exit 2; }
    read -r -s -p "password: " PASS; echo
  fi
  LOGIN_CODE="$(curl -sS --max-time 10 -c "$JAR" -o /dev/null -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    --data-binary @<(python3 -c 'import json,sys; print(json.dumps({"username":sys.argv[1],"password":sys.argv[2]}))' "$USER_NAME" "$PASS") \
    "$RELAY/auth/login")"
  [[ "$LOGIN_CODE" == "200" ]] || { echo "sign-in failed (HTTP $LOGIN_CODE)" >&2; exit 2; }
  CODE="$(fetch "$OUT")"
fi

[[ "$CODE" == "200" ]] || { echo "GET $URL -> HTTP $CODE" >&2; cat "$OUT" >&2; exit 1; }

if $JSON_ONLY; then cat "$OUT"; echo; exit 0; fi

RELAY="$RELAY" python3 - "$OUT" <<'PY'
import json, os, sys

d = json.load(open(sys.argv[1]))
o = d["overall"]


def dur(s):
    s = int(s)
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


print(f"priority lane — {os.environ['RELAY']}")
boots = d.get("boots", 1)
span = f"{dur(d['window_s'])} across {boots} relay start{'s' if boots != 1 else ''}"
print(f"window {span}   (this process up {dur(d['uptime_s'])})")
print(f"caps   {d['cap_total']} total / {d['cap_background']} background, per account")
if d.get("persist_error"):
    print(f"⚠️  persistence: {d['persist_error']}")
if not d.get("persisted_to"):
    print("⚠️  persistence OFF — these counters die with this process")
print()

calls, blocked = o["calls"], o["blocked"]
if calls == 0:
    print("VERDICT  no lane traffic recorded in this window — nothing to conclude.")
elif blocked == 0:
    print(f"VERDICT  no queueing. {calls:,} lane entries, none ever waited.")
    print("         On this evidence a bounded acquire has nothing to fix.")
else:
    print(f"VERDICT  {o['blocked_rate'] * 100:.2f}% queued — {blocked:,} of {calls:,} entries waited.")
    print(f"         worst single wait {o['wait_s_max']:.2f}s, "
          f"average wait when blocked {o['wait_s_avg_blocked']:.2f}s")
    worst = max(d["accounts"].items(), key=lambda kv: kv[1]["wait_s_max"], default=None)
    if worst:
        print(f"         worst account {worst[0]} (max {worst[1]['wait_s_max']:.2f}s)")

rows = [(a, r) for a, r in d["accounts"].items() if r["calls"]]
if rows:
    print()
    print(f"{'account':<16}{'calls':>9}{'queued':>8}{'rate':>8}{'avg wait':>10}{'worst':>9}{'peak':>7}")
    for aid, r in rows:
        peak = f"{r['in_flight_peak']}/{d['cap_total']}"
        avg = f"{r['wait_s_avg_blocked']:.2f}s" if r["blocked"] else "–"
        wor = f"{r['wait_s_max']:.2f}s" if r["blocked"] else "–"
        print(f"{aid:<16}{r['calls']:>9,}{r['blocked']:>8,}"
              f"{r['blocked_rate'] * 100:>7.1f}%{avg:>10}{wor:>9}{peak:>7}")
PY
