#!/usr/bin/env bash
# sanity-test-public.sh — simulate a brand-new user's first run of the
# public repo (BlackPhoenixSlo/chatterly), with zero pre-existing state.
#
# Why this exists: we deploy chatterly-public to the VPS, but our laptop
# normally runs FasttExtension. If chatterly-public diverges and
# breaks the "fresh clone" path (missing module, broken Dockerfile,
# unseeded DB schema, etc.) we'd only find out when the next VPS deploy
# fails halfway through. This script catches that on the laptop, in
# isolation from your live local stack.
#
# What it does:
#   1. Shallow-clones github.com/BlackPhoenixSlo/chatterly into a
#      throwaway dir under /tmp.
#   2. Pre-creates the bind-mount targets a new user would touch:
#         service/proxies.json   (empty registry)
#         service/chatterly.db   (zero-byte; relay creates schema on boot)
#         service/sessions/      (empty — no captured OF auth)
#         service/storyboard_cache/
#   3. Boots the stack with `docker compose up -d --build`, isolated by:
#         COMPOSE_PROJECT_NAME=chatterly-sanity   (separate container names)
#         ports remapped to 8788 / 3002          (so port 8787/3001 stay free
#                                                  for your real local stack)
#   4. Probes the endpoints a new user's browser would hit:
#         /health                 expect 503 (no accounts yet — but the relay
#                                  must respond, not time out)
#         /admin/employees + tok  expect 200 with empty `employees: []`
#                                  (DB schema created OK)
#         /admin/accounts  + tok  expect 200 with empty `accounts:  []`
#         /ui/                    expect 200 — this is the relay's built-in
#                                  static UI from web/ that hosts the paste-
#                                  curl session-bootstrap form. THE actual
#                                  new-user onboarding entry point: /inbox
#                                  needs an account loaded, but /ui/ doesn't.
#                                  Pasting a curl there hits POST
#                                  /admin/session/bootstrap to create the
#                                  first session, after which /health flips
#                                  to 200 and /inbox becomes usable.
#         Next /                  expect 200 (Next.js prod build serves)
#   5. Prints PASS/FAIL per check and an overall summary.
#   6. Tears down via `docker compose down` and deletes the scratch dir
#      (unless --keep).
#
# Usage:
#   ./scripts/sanity-test-public.sh              # build, test, teardown
#   ./scripts/sanity-test-public.sh --keep       # leave the stack running so
#                                                 you can poke at it manually
#   ./scripts/sanity-test-public.sh --seed       # copy your local sessions/
#                                                 proxies/db into the sanity
#                                                 stack instead of starting
#                                                 empty — useful to test the
#                                                 "already onboarded" path
#                                                 end-to-end. Skips the
#                                                 empty-state checks since
#                                                 /health will 200 with
#                                                 accounts loaded.
#   ./scripts/sanity-test-public.sh --branch X   # test a non-main branch
#
# Exit code: 0 if all checks pass, 1 if any failed.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/BlackPhoenixSlo/chatterly.git}"
BRANCH="main"
KEEP=0
SEED=0
SCRATCH="/tmp/chatterly-sanity-$$"
PROJECT="chatterly-sanity"
RELAY_PORT=8788
NEXT_PORT=3002
# Default SHARE_TOKEN baked into docker-compose.yml — works because we
# don't write a .env, so compose falls back to this value.
TOKEN=""
# Where to copy seed state from when --seed is passed. Defaults to the
# FasttExtension working tree (this script's repo root); override
# with SEED_SRC=/some/other/path if you have an alternative copy.
SEED_SRC="${SEED_SRC:-$(cd "$(dirname "$0")/.." && pwd)}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep)    KEEP=1; shift ;;
    --seed)    SEED=1; shift ;;
    --branch)  BRANCH="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,40p' "$0"
      exit 0
      ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[36m▸ %s\033[0m\n' "$*"; }
ok()  { printf '\033[32m  ✓ %s\033[0m\n' "$*"; }
ko()  { printf '\033[31m  ✗ %s\033[0m\n' "$*"; }

PASS=0; FAIL=0
record() {
  if [[ "$1" == "ok" ]]; then PASS=$((PASS+1)); ok "$2"; else FAIL=$((FAIL+1)); ko "$2"; fi
}

cleanup() {
  # Detect whether any containers actually came up. If compose failed
  # before creating containers, --keep would leave a stale scratch dir
  # with no stack to "keep" — clean it up regardless.
  local has_containers=0
  if command -v docker >/dev/null 2>&1; then
    if docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.ID}}' 2>/dev/null | grep -q .; then
      has_containers=1
    fi
  fi

  if [[ $KEEP -eq 1 && $has_containers -eq 1 ]]; then
    cat <<EOF

────────────────────────────────────────────────────────────
Leaving the sanity stack running (--keep). To tear down later:

  cd $SCRATCH && \\
    COMPOSE_PROJECT_NAME=$PROJECT docker compose down && \\
    rm -rf $SCRATCH

URLs while it's up:
  relay:  http://127.0.0.1:$RELAY_PORT/health?t=$TOKEN
  next:   http://127.0.0.1:$NEXT_PORT/inbox?t=$TOKEN
────────────────────────────────────────────────────────────
EOF
    return 0
  fi
  if [[ $KEEP -eq 1 && $has_containers -eq 0 ]]; then
    say "--keep was set but no containers came up — cleaning scratch anyway"
  else
    say "tearing down ($PROJECT) and removing $SCRATCH"
  fi
  if [[ -d "$SCRATCH" ]]; then
    ( cd "$SCRATCH" && COMPOSE_PROJECT_NAME="$PROJECT" docker compose down --volumes --remove-orphans >/dev/null 2>&1 ) || true
    rm -rf "$SCRATCH"
  fi
}
trap cleanup EXIT

# ── 1. clone ─────────────────────────────────────────────────────
say "shallow-cloning $REPO_URL ($BRANCH) into $SCRATCH"
git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$SCRATCH" >/dev/null
cd "$SCRATCH"
echo "  HEAD: $(git log --oneline -1)"

# ── 2. pre-create bind-mount targets (the new-user state) ───────
if [[ $SEED -eq 1 ]]; then
  say "seeding state from $SEED_SRC/service/  (--seed)"
  for f in proxies.json chatterly.db; do
    [ -f "$SEED_SRC/service/$f" ] || { ko "missing $SEED_SRC/service/$f"; exit 1; }
    cp "$SEED_SRC/service/$f" "service/$f"
  done
  mkdir -p service/sessions service/storyboard_cache
  # Only the actual session blobs — skip capture-only dirs (audit/,
  # browser_profiles/, crawl/, etc.) that bloat the copy with megabytes
  # of replay data the runtime doesn't read.
  [ -d "$SEED_SRC/service/sessions/accounts" ] && \
    cp -r "$SEED_SRC/service/sessions/accounts" service/sessions/
  for f in active.json latest.json .migrated; do
    [ -f "$SEED_SRC/service/sessions/$f" ] && cp "$SEED_SRC/service/sessions/$f" service/sessions/
  done
  echo "  seeded $(find service/sessions/accounts -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ') account(s)"
else
  say "creating empty state files (no sessions, no proxies, no db)"
  echo '{"proxies":[]}' > service/proxies.json
  touch service/chatterly.db
  mkdir -p service/sessions service/storyboard_cache
fi

# docker-compose.yml requires SHARE_TOKEN with no default fallback.
# A brand-new user who just types `docker compose up` cold hits this
# same "required variable SHARE_TOKEN is missing a value" error — that
# IS a real new-user gotcha worth noting, but the README does say
# "Pick a fresh SHARE_TOKEN" before booting. We write the same token
# the deploy-vps.sh would write so the rest of the sanity checks run.
echo "SHARE_TOKEN=$TOKEN" > .env
chmod 600 .env

# ── 3. remap ports + boot ───────────────────────────────────────
say "remapping ports to $RELAY_PORT / $NEXT_PORT and stripping container_name: lines"
# In-place edits to allow this sanity stack to coexist with the user's
# live stack on the same machine:
#   • Remap ports 8787→$RELAY_PORT, 3001→$NEXT_PORT (avoid port conflict).
#     The 127.0.0.1: prefix stays — sanity stack must not leak to LAN.
#   • Delete every `container_name:` line. chatterly-public hard-codes
#     chatterly-relay / chatterly-app / chatterly-autoheal / chatterly-
#     tunnel as container names, which OVERRIDES COMPOSE_PROJECT_NAME's
#     auto-prefixing. Result: even a different project tries to claim
#     names that the user's live stack already holds, and docker errors
#     out ("name in use"). Stripping these lines lets compose auto-name
#     as ${PROJECT}-${SERVICE}-1.
sed -i.bak \
  -e "s|127\.0\.0\.1:8787:8787|127.0.0.1:$RELAY_PORT:8787|" \
  -e "s|127\.0\.0\.1:3001:3001|127.0.0.1:$NEXT_PORT:3001|" \
  -e "/^[[:space:]]*container_name:/d" \
  docker-compose.yml
rm docker-compose.yml.bak

say "attempt 1: normal 'docker compose up -d --build' (the new-user happy path)"
# Capture output AND exit code separately. pipefail+tail would mask the real
# exit code; we want to know whether compose itself blew up vs. just noisy logs.
set +e
COMPOSE_PROJECT_NAME="$PROJECT" docker compose up -d --build 2>&1 | tee /tmp/sanity-up.log | tail -20
UP_RC=${PIPESTATUS[0]}
set -e

if [[ $UP_RC -eq 0 ]]; then
  record ok "compose up -d --build succeeded — new-user happy path works"
elif grep -q "dependency failed to start" /tmp/sanity-up.log && grep -q "is unhealthy" /tmp/sanity-up.log; then
  # The big one. Worth surfacing very loudly because it means the public
  # repo is unbootable by anyone who hasn't already captured a session,
  # which is by definition every brand-new user. The UI they'd need to
  # capture a session in is exactly what's blocked.
  record ko "compose up FAILED: app.depends_on.relay.condition is service_healthy, but relay /health returns 503 when 0 accounts are loaded → app never starts. New users are deadlocked: they can't reach /inbox to capture a session because the UI container is waiting on a relay that won't become healthy until a session exists. Fix in chatterly-public/docker-compose.yml: change app's depends_on condition to service_started, OR change relay's healthcheck so /health returns 200 with 0 accounts (just confirms the process is alive)."
  say "falling back to --no-deps so we can still probe relay + app individually"
  COMPOSE_PROJECT_NAME="$PROJECT" docker compose up -d --no-deps relay autoheal 2>&1 | tail -5
  COMPOSE_PROJECT_NAME="$PROJECT" docker compose up -d --no-deps app 2>&1 | tail -5
else
  record ko "compose up failed for an unexpected reason — last 20 lines saved to /tmp/sanity-up.log"
  exit 1
fi

# ── 4. probe ─────────────────────────────────────────────────────
say "waiting up to 90s for the relay to answer (any HTTP status is fine — we just want it to not time out)"
# Give uvicorn a head start on importing modules + binding the port.
# The first curl right after container start otherwise sees "Empty reply"
# while uvicorn is still warming up, and would noisily false-positive.
sleep 5
for i in $(seq 1 30); do
  # Discard stdout entirely; use --write-out to a separate fd so the -w
  # template can't co-mingle with the `|| echo` fallback. Result: $CODE
  # is exactly one of {a valid 3-digit HTTP code, the string "000"}.
  CODE=$(curl -sS -o /dev/null --max-time 3 -w '%{http_code}' "http://127.0.0.1:$RELAY_PORT/health" 2>/dev/null)
  if [[ "$CODE" =~ ^[1-5][0-9][0-9]$ ]]; then
    echo "  relay responding after ${i}×3s (status $CODE)"
    break
  fi
  sleep 3
  [[ $i -eq 30 ]] && { ko "relay never responded — last 30 lines:"; docker logs --tail 30 "${PROJECT}-relay-1" 2>&1; exit 1; }
done

say "running checks"

# /health — with no sessions: 503 + structured body (the "remedy" field
# tells new users to POST /admin/session/bootstrap). With --seed: 200 +
# account info. Either flips of the right shape is a pass.
CODE=$(curl -sS -o /tmp/sanity-health.txt -w '%{http_code}' --max-time 5 "http://127.0.0.1:$RELAY_PORT/health?t=$TOKEN")
if [[ $SEED -eq 1 && "$CODE" == "200" ]] && grep -q '"ok":true' /tmp/sanity-health.txt; then
  record ok "/health responds 200 with --seed (account loaded as expected)"
elif [[ $SEED -eq 0 && "$CODE" == "503" ]] && grep -q 'remedy' /tmp/sanity-health.txt; then
  record ok "/health responds 503 with remedy hint (correct new-user signal — points at POST /admin/session/bootstrap)"
elif [[ "$CODE" == "200" ]]; then
  record ok "/health responds 200 — relay reports an account is loaded"
elif [[ "$CODE" == "503" ]]; then
  record ok "/health responds 503 — no sessions loaded (expected for empty boot)"
else
  record ko "/health: HTTP $CODE — expected 200 or 503. Body:"
  cat /tmp/sanity-health.txt; echo
fi

# /admin/employees — expect 200 with empty list (DB schema created OK).
CODE=$(curl -sS -o /tmp/sanity-emp.txt -w '%{http_code}' --max-time 5 "http://127.0.0.1:$RELAY_PORT/admin/employees?t=$TOKEN")
if [[ "$CODE" == "200" ]] && grep -q '"employees"' /tmp/sanity-emp.txt; then
  record ok "/admin/employees: 200, returns {\"employees\": [...]} (DB schema OK)"
else
  record ko "/admin/employees: HTTP $CODE. Body: $(cat /tmp/sanity-emp.txt)"
fi

# /admin/accounts — expect 200 with empty list.
CODE=$(curl -sS -o /tmp/sanity-acc.txt -w '%{http_code}' --max-time 5 "http://127.0.0.1:$RELAY_PORT/admin/accounts?t=$TOKEN")
if [[ "$CODE" == "200" ]] && grep -q '"accounts"' /tmp/sanity-acc.txt; then
  record ok "/admin/accounts: 200, returns {\"accounts\": [...]}"
else
  record ko "/admin/accounts: HTTP $CODE. Body: $(cat /tmp/sanity-acc.txt)"
fi

# Bad-token check — relay's share-token gate should reject.
CODE=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$RELAY_PORT/admin/employees?t=wrong-token")
if [[ "$CODE" == "401" || "$CODE" == "403" ]]; then
  record ok "/admin/* with wrong token rejected with HTTP $CODE (share-token gate works)"
else
  record ko "/admin/* with wrong token returned HTTP $CODE — gate is broken!"
fi

# /ui/ — the relay's built-in static UI (served from web/) that hosts the
# paste-curl bootstrap form. This is THE new-user onboarding entry point:
# /inbox needs an account loaded, but /ui/ works with zero sessions, and
# pasting a curl there hits POST /admin/session/bootstrap to create the
# first one. If /ui/ ever breaks, brand-new users have no way in.
CODE=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$RELAY_PORT/ui/?t=$TOKEN")
if [[ "$CODE" == "200" ]]; then
  record ok "/ui/ responds 200 — new-user bootstrap UI reachable with zero sessions"
else
  record ko "/ui/: HTTP $CODE — new users have no onboarding path!"
fi

# Next.js — the prod build's home should serve. May take a beat after relay
# came up since the app container depends_on relay's healthcheck — but relay
# is 503 (unhealthy) on a fresh boot, so depends_on will block forever.
# We work around by giving app a longer probe window.
say "waiting for Next.js (longer — it's wedged on relay healthcheck until we cancel it)"
NEXT_OK=0
for i in $(seq 1 30); do
  CODE=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$NEXT_PORT/" 2>/dev/null || echo "000")
  if [[ "$CODE" =~ ^[23] ]]; then NEXT_OK=1; break; fi
  sleep 2
done
if [[ $NEXT_OK -eq 1 ]]; then
  record ok "Next.js / responds (status $CODE) — UI is serving"
else
  # depends_on: condition: service_healthy is the issue — relay 503's
  # because no sessions, so the app container never starts. This is a
  # *real* problem for a brand-new user; flag it.
  record ko "Next.js never came up in 60s. Most likely cause: docker-compose.yml has app.depends_on.relay = service_healthy, but relay 503's on a fresh boot (no sessions). Means a new user can't start the UI until they capture a session via the loginExtension flow."
fi

# ── 5. summary ──────────────────────────────────────────────────
echo
if [[ $FAIL -eq 0 ]]; then
  printf '\033[32m✓ sanity passed: %d/%d checks\033[0m\n' "$PASS" "$((PASS+FAIL))"
  exit 0
else
  printf '\033[31m✗ sanity failed: %d failures (%d passed)\033[0m\n' "$FAIL" "$PASS"
  echo
  echo "Recent relay logs:"
  docker logs --tail 20 "${PROJECT}-relay-1" 2>&1 | sed 's/^/  /'
  exit 1
fi
