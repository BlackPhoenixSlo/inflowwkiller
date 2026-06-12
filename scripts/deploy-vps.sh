#!/usr/bin/env bash
#
# deploy-vps.sh — deploy inflowwkiller to a Hostinger (or any Ubuntu/Debian) VPS.
#
# Read DEPLOY_HOWTO.md FIRST. For a vanilla deploy you change ONE line below —
# SSH_TARGET (your VPS IP) — and fill in your secrets (DeepSeek key + two
# `openssl rand -hex 32` values). The repo URL already points at the public
# upstream, so no GitHub fork is needed. Everything after the edit block is
# mechanical.
#
# What it does, in order:
#   1. Verify SSH to the host.
#   2. Install Docker + compose plugin (idempotent).
#   3. Clone (or git-pull) the repo into ~/$REMOTE_DIR at $BRANCH.
#   4. Pre-create bind-mount targets (proxies.json, chatterly.db, caches).
#   5. Optionally scp local state (sessions / proxies / db) to the host.
#   6. Write/refresh ~/$REMOTE_DIR/.env with SHARE_TOKEN + the secrets below.
#   7. docker compose --profile tunnel up -d --build, then print the URL.
#
# Usage:
#   ./scripts/deploy-vps.sh                 # uses the config block below
#   ./scripts/deploy-vps.sh root@1.2.3.4    # override the SSH target
#   ./scripts/deploy-vps.sh root@1.2.3.4 --branch main --no-state

set -euo pipefail

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  EDIT BEFORE RUNNING                                                       ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# --- Where to deploy  (★ THE ONE LINE YOU MUST CHANGE ★) --------------------
SSH_TARGET="root@YOUR_VPS_IP"          # your VPS, e.g. root@203.0.113.7
SSH_PORT=22

# --- What to deploy ---------------------------------------------------------
# Defaults to the PUBLIC upstream, so a vanilla deploy needs no GitHub fork —
# the VPS clones this repo directly. Point these at your own fork ONLY if you
# want to deploy customized code.
REMOTE_REPO_URL="https://github.com/BlackPhoenixSlo/inflowwkiller.git"
REMOTE_DIR="inflowwkiller"             # clones into ~/inflowwkiller on the VPS
BRANCH="main"

# --- Secrets written into the VPS .env (NEVER commit these) -----------------
# Leave a value EMPTY to keep whatever is already in the VPS .env (so a
# redeploy doesn't clobber keys you set once). Generate fresh ones with:
#   openssl rand -hex 32
SESSION_SECRET=""                      # cookie signing key (set once, keep it)
CHATTER_SESSION_SECRET=""              # chatter cookie signing key
DEEPSEEK_API_KEY=""                    # house default LLM provider
GROK_API_KEY=""                        # optional / legacy
SHARE_TOKEN=""                         # empty = share-gate OFF (friend-auth only)

# --- Pull local state to the VPS on this run? -------------------------------
# 1 = scp your laptop's service/sessions + proxies.json + chatterly.db up
#     (the relay boots already authenticated).
# 0 = fresh box; capture a session via the Chrome extension after boot.
# The user's plan is to send DB/secrets SEPARATELY — default OFF.
COPY_STATE=0

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  END EDIT BLOCK — you shouldn't need to change anything below             ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# CLI overrides (optional)
[[ $# -ge 1 && "$1" != --* ]] && { SSH_TARGET="$1"; shift; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)     SSH_PORT="$2"; shift 2 ;;
    --branch)   BRANCH="$2"; shift 2 ;;
    --state)    COPY_STATE=1; shift ;;
    --no-state) COPY_STATE=0; shift ;;
    -h|--help)  grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done

LOCAL_REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SSH=(ssh -p "$SSH_PORT" -o StrictHostKeyChecking=accept-new "$SSH_TARGET")
SCP=(scp -P "$SSH_PORT" -o StrictHostKeyChecking=accept-new)
say() { printf '\n\033[36m▸ %s\033[0m\n' "$*"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[[ "$SSH_TARGET" == *YOUR_VPS_IP* ]] && die "edit SSH_TARGET (or pass root@<ip> as the first arg) — still the placeholder"

# ---------- 1. SSH reachability ----------
say "checking SSH to $SSH_TARGET:$SSH_PORT"
"${SSH[@]}" -o ConnectTimeout=10 true || die "cannot SSH — check IP, port, and your public key on the host"

# ---------- 2. Docker install ----------
say "installing Docker (idempotent)"
"${SSH[@]}" bash -s <<'REMOTE'
set -euo pipefail
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
docker compose version >/dev/null 2>&1 || { echo "✗ docker compose plugin missing" >&2; exit 1; }
docker --version; docker compose version
REMOTE

# ---------- 3. Clone / pull ----------
say "cloning $REMOTE_REPO_URL into ~/$REMOTE_DIR (branch: $BRANCH)"
"${SSH[@]}" bash -s <<REMOTE
set -euo pipefail
if [ -d "\$HOME/$REMOTE_DIR/.git" ]; then
  cd "\$HOME/$REMOTE_DIR"; git fetch origin; git checkout "$BRANCH"; git pull --ff-only
else
  git clone --branch "$BRANCH" "$REMOTE_REPO_URL" "\$HOME/$REMOTE_DIR"
fi
REMOTE

# ---------- 4. Bind-mount targets ----------
say "creating bind-mount targets"
"${SSH[@]}" bash -s <<REMOTE
set -euo pipefail
cd "\$HOME/$REMOTE_DIR"
[ -f service/proxies.json ] || echo '{"proxies":[]}' > service/proxies.json
[ -f service/chatterly.db ] || touch service/chatterly.db
mkdir -p service/sessions service/storyboard_cache
REMOTE

# ---------- 5. Optional state transfer ----------
if [[ $COPY_STATE -eq 1 ]]; then
  say "copying local sessions / proxies.json / chatterly.db to the VPS"
  L_S="$LOCAL_REPO_ROOT/service/sessions"; L_P="$LOCAL_REPO_ROOT/service/proxies.json"; L_DB="$LOCAL_REPO_ROOT/service/chatterly.db"
  [ -d "$L_S" ] && "${SCP[@]}" "$L_S"/*.json "$SSH_TARGET:~/$REMOTE_DIR/service/sessions/" 2>/dev/null || echo "  (no top-level session json)"
  [ -d "$L_S/accounts" ] && "${SCP[@]}" -r "$L_S/accounts" "$SSH_TARGET:~/$REMOTE_DIR/service/sessions/" || echo "  (no accounts/ — boots with 0 accounts)"
  [ -f "$L_P" ]  && "${SCP[@]}" "$L_P"  "$SSH_TARGET:~/$REMOTE_DIR/service/proxies.json" || true
  [ -f "$L_DB" ] && "${SCP[@]}" "$L_DB" "$SSH_TARGET:~/$REMOTE_DIR/service/chatterly.db" || true
else
  say "skipping state transfer (COPY_STATE=0) — send DB/sessions/secrets separately, or capture via the Chrome extension after boot"
fi

# ---------- 6. .env (merge: only overwrite keys you set above) ----------
say "writing ~/$REMOTE_DIR/.env (empty values keep the existing VPS value)"
"${SSH[@]}" "REMOTE_DIR='$REMOTE_DIR' SHARE_TOKEN='$SHARE_TOKEN' SESSION_SECRET='$SESSION_SECRET' CHATTER_SESSION_SECRET='$CHATTER_SESSION_SECRET' DEEPSEEK_API_KEY='$DEEPSEEK_API_KEY' GROK_API_KEY='$GROK_API_KEY' bash -s" <<'REMOTE'
set -euo pipefail
cd "$HOME/$REMOTE_DIR"; touch .env; chmod 600 .env
upsert() { # upsert KEY VALUE — only if VALUE non-empty; else leave as-is
  local k="$1" v="$2"
  [ -z "$v" ] && return 0
  if grep -q "^$k=" .env; then sed -i "s|^$k=.*|$k=$v|" .env; else printf '%s=%s\n' "$k" "$v" >> .env; fi
}
upsert SHARE_TOKEN "$SHARE_TOKEN"
upsert SESSION_SECRET "$SESSION_SECRET"
upsert CHATTER_SESSION_SECRET "$CHATTER_SESSION_SECRET"
upsert DEEPSEEK_API_KEY "$DEEPSEEK_API_KEY"
upsert GROK_API_KEY "$GROK_API_KEY"
grep -q '^SHARE_TOKEN=' .env || echo 'SHARE_TOKEN=' >> .env
echo "  .env keys: $(grep -oE '^[A-Z_]+' .env | tr '\n' ' ')"
REMOTE

# ---------- 7. compose up + tunnel URL ----------
say "docker compose --profile tunnel up -d --build (first build takes a few min)"
"${SSH[@]}" bash -s <<REMOTE
set -euo pipefail
cd "\$HOME/$REMOTE_DIR"
docker compose --profile tunnel up -d --build
docker compose ps
REMOTE

say "waiting for the cloudflared public URL"
TUNNEL_URL=""
for _ in $(seq 1 30); do
  TUNNEL_URL=$("${SSH[@]}" "docker logs chatterly-tunnel 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1" || true)
  [[ -n "$TUNNEL_URL" ]] && break; printf '.'; sleep 4
done; echo

if [[ -z "$TUNNEL_URL" ]]; then
  echo "⚠ tunnel URL not seen in 2 min. Tail: ssh -p $SSH_PORT $SSH_TARGET 'cd ~/$REMOTE_DIR && docker compose logs -f tunnel'"
  exit 1
fi

cat <<DONE

────────────────────────────────────────────────────────────
✓ inflowwkiller is live on ${SSH_TARGET}

  URL:   ${TUNNEL_URL}
  dir:   ~/${REMOTE_DIR}   branch: ${BRANCH}

Redeploy after a new push:   $0 ${SSH_TARGET} --branch ${BRANCH}
New tunnel URL anytime:      ssh -p ${SSH_PORT} ${SSH_TARGET} 'docker logs chatterly-tunnel | grep trycloudflare | tail -1'
Take the public URL down:    ssh -p ${SSH_PORT} ${SSH_TARGET} 'cd ~/${REMOTE_DIR} && docker compose --profile tunnel stop tunnel'
────────────────────────────────────────────────────────────
DONE
