#!/usr/bin/env bash
# Pull N random conversations (last 20 messages each) from prod and print them.
# Read-only. One ssh call, nothing written anywhere.
#
#   scripts/convo-check.sh              # 4 convos, last 20 msgs, fan replied in 24h
#   scripts/convo-check.sh --n 6
#   scripts/convo-check.sh --hours 4    # only threads active in the last 4h
set -euo pipefail
HOST="${CONVO_COACH_HOST:-root@YOUR_VPS_IP}"
CONTAINER="${CONVO_COACH_CONTAINER:-fastt-relay}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKIP="${CONVO_COACH_SKIP:-}"
TMP="/tmp/convo-check.$$"

trap 'ssh "$HOST" "rm -rf $TMP; docker exec '"$CONTAINER"' rm -f /tmp/convo_check_fetch.py" 2>/dev/null || true' EXIT

ssh "$HOST" "mkdir -p $TMP"
scp -q "$REPO/service/convo_check_fetch.py" "$HOST:$TMP/"
ssh "$HOST" "docker cp $TMP/convo_check_fetch.py $CONTAINER:/tmp/"
ssh "$HOST" "docker exec $CONTAINER python /tmp/convo_check_fetch.py ${SKIP:+--skip-accounts '$SKIP'} $*"
