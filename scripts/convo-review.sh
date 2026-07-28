#!/usr/bin/env bash
# Build (or rebuild) the convo reader — one self-contained HTML of the
# conversations worth reading, pulled from prod.
#
# Usage: scripts/convo-review.sh [--text-only|--render-only] [--no-open] [-- <args>]
#
#   scripts/convo-review.sh                       # the usual: refresh everything
#   scripts/convo-review.sh --text-only           # no OF calls, no pictures, fast
#   scripts/convo-review.sh -- --selling 40 --breaks 5
#   scripts/convo-review.sh -- --window 2000      # keep more of each thread
#   scripts/convo-review.sh --render-only         # rebuild the page, no VPS
#   scripts/convo-review.sh --render-only -- --max-mb 150   # embed more pictures
#
# Re-run it whenever you want the picture updated — that is the point. The
# selection is recomputed from live prod each time, and the media cache under
# ~/.convo-review/media means a re-run only pays for pictures it hasn't seen.
#
# WHY IT IS SHAPED LIKE THIS
# Every OF CDN url is signed with an expiry (~2 days) AND the account proxy's
# source IP, so a viewer that renders stored urls renders nothing on a laptop.
# The only durable copy is the bytes, and the only place that can fetch them is
# a process inside fastt-relay using the account's own client. So the read runs
# up there (read-only: sqlite mode=ro, GETs only), ships JSON down, and the HTML
# is assembled here.
#
# Nothing about this writes to prod. It copies a script into the container's
# /tmp, runs it, copies the result out, and deletes both.
set -euo pipefail

HOST="root@YOUR_VPS_IP"
CONTAINER="fastt-relay"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOME_DIR="${CONVO_REVIEW_HOME:-$HOME/.convo-review}"
CACHE="$HOME_DIR/media"
OUT="${CONVO_REVIEW_OUT:-$REPO/convo_review.html}"

FETCHER="$REPO/service/convo_review_fetch.py"
RENDERER="$REPO/service/convo_review_render.py"
REMOTE_TMP="/tmp/convo-review.$$"
OPEN_IT=1
EXTRA=()

RENDER_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --text-only)   EXTRA+=("--no-media"); shift ;;
    --render-only) RENDER_ONLY=1; shift ;;
    --no-open)   OPEN_IT=0; shift ;;
    --)          shift; EXTRA+=("$@"); break ;;
    -h|--help)   sed -n '2,27p' "$0"; exit 0 ;;
    *)           EXTRA+=("$1"); shift ;;
  esac
done

mkdir -p "$CACHE"

# Rebuild the page from the last pull — no VPS, no OF, instant. For when you
# want a different look or a wider budget, not fresher conversations.
if [[ "$RENDER_ONLY" == 1 ]]; then
  [[ -f "$HOME_DIR/payload.json" ]] || { echo "no previous pull in $HOME_DIR" >&2; exit 1; }
  python3 "$RENDERER" "$HOME_DIR/payload.json" --cache "$CACHE" --out "$OUT" ${EXTRA[@]+"${EXTRA[@]}"}
  if [[ "$OPEN_IT" == 1 ]] && command -v open >/dev/null; then open "$OUT"; fi
  exit 0
fi

# 1. What the laptop already holds — the fetcher skips re-downloading these.
echo "→ checking the local media cache…"
python3 "$RENDERER" /dev/null --cache "$CACHE" --out /dev/null \
        --have "$HOME_DIR/have.json"
echo "  $(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))))' \
        "$HOME_DIR/have.json") pictures already cached"

# 2. Ship the reader + the have-list up, and into the container.
echo "→ sending the reader to $HOST…"
ssh "$HOST" "mkdir -p $REMOTE_TMP"
scp -q "$FETCHER" "$HOME_DIR/have.json" "$HOST:$REMOTE_TMP/"
ssh "$HOST" "docker cp $REMOTE_TMP/convo_review_fetch.py $CONTAINER:/tmp/ && \
             docker cp $REMOTE_TMP/have.json $CONTAINER:/tmp/"

# 3. Read prod. Progress streams on stderr; the JSON stays in the container.
echo "→ reading prod (this pages OF for fresh media urls — a few minutes)…"
ssh "$HOST" "docker exec -w /app/service $CONTAINER python /tmp/convo_review_fetch.py \
             --out /tmp/convo_review.json --have /tmp/have.json ${EXTRA[*]:-}"

# 4. Bring it home and clean up after ourselves.
echo "→ downloading…"
ssh "$HOST" "docker cp $CONTAINER:/tmp/convo_review.json $REMOTE_TMP/convo_review.json"
scp -q "$HOST:$REMOTE_TMP/convo_review.json" "$HOME_DIR/payload.json"
ssh "$HOST" "rm -rf $REMOTE_TMP; \
             docker exec $CONTAINER rm -f /tmp/convo_review_fetch.py /tmp/have.json \
                                          /tmp/convo_review.json" || true

# 5. Fold the new media into the cache and write the one file.
echo "→ building $OUT…"
python3 "$RENDERER" "$HOME_DIR/payload.json" --cache "$CACHE" --out "$OUT"

if [[ "$OPEN_IT" == 1 ]] && command -v open >/dev/null; then open "$OUT"; fi
echo "done."
