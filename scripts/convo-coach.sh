#!/usr/bin/env bash
# The convo coach — every four hours, read a few real conversations and ask
# what could be better.
#
# Usage: scripts/convo-coach.sh [options]
#
#   scripts/convo-coach.sh                  # one batch, now (what launchd runs)
#   scripts/convo-coach.sh --hours 12       # widen the window
#   scripts/convo-coach.sh --max 4          # smaller batch
#   scripts/convo-coach.sh --no-review      # sample + transcripts, skip Claude
#   scripts/convo-coach.sh --last           # print the newest review, no work
#   scripts/convo-coach.sh --open           # ...and open the transcripts page
#   scripts/convo-coach.sh --install        # install the 4-hourly launchd job
#   scripts/convo-coach.sh --uninstall
#
# WHAT IT DOES
#   1. Inside fastt-relay (read-only), find threads where a fan REPLIED in the
#      last few hours and the exchange has since finished — half that went
#      nowhere, a couple that closed. Skips anything still in motion, and
#      anything reviewed in the last fortnight.
#   2. Bring the transcripts down and write them as markdown + a viewer page.
#   3. Hand them to `claude -p` with a rubric, plus the last three reviews so it
#      stops repeating itself.
#   4. Drop the lot in reports/convo-coach/<batch>/ and print the findings.
#
# Everything lands in ONE folder per batch:
#   review.md    the findings — this is the thing to read
#   convos.html  the transcripts, as a chat viewer
#   convos.md    the transcripts, as the reviewer saw them
#   prompt.md    exactly what was asked (so a finding can be audited later)
#   pack.json    the raw payload
#
# NOTHING HERE WRITES TO PRODUCTION. The DB is opened mode=ro, the scripts are
# copied into the container's /tmp and deleted afterwards, and no OF calls are
# made at all — the coach reads what is already stored, which is what makes it
# cheap enough to run six times a day.
#
# The batch folder is gitignored: it holds verbatim fan transcripts. Read it on
# the laptop, never publish it.
set -euo pipefail

HOST="${CONVO_COACH_HOST:-root@YOUR_VPS_IP}"
CONTAINER="${CONVO_COACH_CONTAINER:-fastt-relay}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="${CONVO_COACH_ROOT:-$REPO/reports/convo-coach}"
STATE="$ROOT/state"
SEEN="$STATE/seen.json"

FETCH_RV="$REPO/service/convo_review_fetch.py"     # imported by the coach fetcher
FETCH="$REPO/service/convo_coach_fetch.py"
REPORT="$REPO/service/convo_coach_report.py"
RENDER="$REPO/service/convo_review_render.py"

PY="${CONVO_COACH_PY:-python3}"
# Opus by default: this job reads six conversations, six times a day, and the
# whole point is the quality of the judgement. Override with CONVO_COACH_MODEL.
MODEL="${CONVO_COACH_MODEL:-opus}"

REVIEW=1
OPEN_IT=0
EXTRA=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-review) REVIEW=0; shift ;;
    --open)      OPEN_IT=1; shift ;;
    --last)      MODE=last; shift ;;
    --install)   MODE=install; shift ;;
    --uninstall) MODE=uninstall; shift ;;
    -h|--help)   sed -n '2,40p' "$0"; exit 0 ;;
    --)          shift; EXTRA+=("$@"); break ;;
    *)           EXTRA+=("$1"); shift ;;
  esac
done
MODE="${MODE:-run}"

PLIST="$HOME/Library/LaunchAgents/com.chatterly.convo-coach.plist"

# ── the small modes ──────────────────────────────────────────────────────────

latest_batch() {
  find "$ROOT" -mindepth 1 -maxdepth 1 -type d ! -name state 2>/dev/null \
    | sort | tail -1
}

if [[ "$MODE" == "last" ]]; then
  B="$(latest_batch)"
  [[ -n "$B" ]] || { echo "no batches yet — run scripts/convo-coach.sh" >&2; exit 1; }
  echo "── $(basename "$B") ──────────────────────────────────────────"
  cat "$B/review.md" 2>/dev/null || echo "(no review.md in $B)"
  [[ "$OPEN_IT" == 1 ]] && command -v open >/dev/null && open "$B/convos.html"
  exit 0
fi

if [[ "$MODE" == "install" ]]; then
  mkdir -p "$(dirname "$PLIST")" "$STATE"
  # StartInterval, not StartCalendarInterval: four hours after the LAST run,
  # not four fixed clock times. On a laptop that sleeps, the calendar form
  # fires every missed slot at once the moment the lid opens — four batches in
  # four seconds, all reading the same window.
  cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.chatterly.convo-coach</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>exec $REPO/scripts/convo-coach.sh</string>
  </array>
  <key>StartInterval</key><integer>14400</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$STATE/coach.log</string>
  <key>StandardErrorPath</key><string>$STATE/coach.log</string>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLIST_EOF
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  echo "installed — a batch every 4h, log at $STATE/coach.log"
  echo "  run one now:  scripts/convo-coach.sh"
  echo "  read it:      scripts/convo-coach.sh --last"
  exit 0
fi

if [[ "$MODE" == "uninstall" ]]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed."
  exit 0
fi

# ── a batch ──────────────────────────────────────────────────────────────────

STAMP="$(date -u +%Y-%m-%dT%H%MZ)"
BATCH="$ROOT/$STAMP"
REMOTE_TMP="/tmp/convo-coach.$$"
mkdir -p "$BATCH" "$STATE"
[[ -f "$SEEN" ]] || echo '{}' > "$SEEN"

# On any failure, take the half-built folder with us. A batch directory holding
# a pack and no review reads, at a glance, exactly like a batch where the
# reviewer found nothing — and that is the one confusion this tool cannot
# afford. Trap covers the ssh/exec failures the `set -e` above turns into exits.
cleanup() {
  local rc=$?
  if [[ $rc -ne 0 && -d "$BATCH" && ! -f "$BATCH/review.md" ]]; then
    rm -rf "$BATCH"
    echo "→ failed; removed the partial batch $STAMP" >&2
  fi
  ssh "$HOST" "rm -rf $REMOTE_TMP" 2>/dev/null || true
  return $rc
}
trap cleanup EXIT

echo "→ $STAMP — reading prod…"
ssh "$HOST" "mkdir -p $REMOTE_TMP"
scp -q "$FETCH" "$FETCH_RV" "$SEEN" "$HOST:$REMOTE_TMP/"
ssh "$HOST" "docker cp $REMOTE_TMP/convo_coach_fetch.py $CONTAINER:/tmp/ && \
             docker cp $REMOTE_TMP/convo_review_fetch.py $CONTAINER:/tmp/ && \
             docker cp $REMOTE_TMP/seen.json $CONTAINER:/tmp/convo_coach_seen.json"

# CONVO_COACH_SKIP is read here and passed explicitly: the fetcher runs inside
# a container on another machine, so the laptop's environment does not reach it.
SKIP_ARG=""
[[ -n "${CONVO_COACH_SKIP:-}" ]] && SKIP_ARG="--skip-accounts '${CONVO_COACH_SKIP}'"

ssh "$HOST" "docker exec $CONTAINER python /tmp/convo_coach_fetch.py \
             --out /tmp/convo_coach.json --seen /tmp/convo_coach_seen.json \
             $SKIP_ARG ${EXTRA[*]:-}"

ssh "$HOST" "docker cp $CONTAINER:/tmp/convo_coach.json $REMOTE_TMP/pack.json"
scp -q "$HOST:$REMOTE_TMP/pack.json" "$BATCH/pack.json"
ssh "$HOST" "docker exec $CONTAINER rm -f /tmp/convo_coach_fetch.py \
             /tmp/convo_review_fetch.py /tmp/convo_coach_seen.json \
             /tmp/convo_coach.json" || true

PICKED=$("$PY" -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["threads"]))' \
         "$BATCH/pack.json")
if [[ "$PICKED" == "0" ]]; then
  # A quiet window is not a failure, and it is not worth a Claude call or a row
  # in the index. Say so and leave nothing behind.
  rm -rf "$BATCH"
  echo "→ nothing finished in the window — no batch. (Try --hours 24.)"
  trap - EXIT; ssh "$HOST" "rm -rf $REMOTE_TMP" 2>/dev/null || true
  exit 0
fi
echo "→ $PICKED conversations"

echo "→ writing transcripts…"
"$PY" "$REPORT" "$BATCH/pack.json" --batch "$BATCH" --root "$ROOT" --stage prompt
"$PY" "$RENDER" "$BATCH/pack.json" --cache "$STATE/media" --out "$BATCH/convos.html" \
      >/dev/null

if [[ "$REVIEW" == 1 ]]; then
  echo "→ reviewing ($MODEL)…"
  # Run from the batch folder, with the writing and network tools off. The
  # prompt is self-contained: everything the reviewer needs is on stdin, and a
  # reviewer that can reach the repo will spend the batch reading source
  # instead of reading conversations. Read/Grep stay on so it can open
  # pack.json for a detail the markdown dropped — inside this folder only.
  claude -p --model "$MODEL" --output-format text \
         --disallowedTools Bash Edit Write NotebookEdit WebFetch WebSearch Task \
         < "$BATCH/prompt.md" > "$BATCH/review.md" \
    || { echo "  ! claude failed — transcripts are still in $BATCH" >&2
         echo "# review failed — transcripts in this folder are still good" \
              > "$BATCH/review.md"; }
else
  echo "# not reviewed (--no-review)" > "$BATCH/review.md"
fi

"$PY" "$REPORT" "$BATCH/pack.json" --batch "$BATCH" --root "$ROOT" --stage finish

ln -sfn "$STAMP" "$ROOT/latest"

echo
echo "════════════════════════════════════════════════════════════════════"
cat "$BATCH/review.md"
echo "════════════════════════════════════════════════════════════════════"
echo
echo "transcripts: $BATCH/convos.html"
echo "index:       $ROOT/index.md"

[[ "$OPEN_IT" == 1 ]] && command -v open >/dev/null && open "$BATCH/convos.html"
exit 0
