#!/usr/bin/env bash
#
# deploy-here.sh — run THIS ON the VPS itself (e.g. Hostinger's browser terminal).
# No laptop, no SSH. It installs Docker, clones/updates the repo, brings the stack
# up behind a Cloudflare quick-tunnel, and prints the public URL.
#
# Zero secrets, zero proxies needed for a brand-new account:
#   • session secrets are generated automatically on first run,
#   • no proxy is required for one account (it uses this server's own IP —
#     proxies only matter once you host MULTIPLE OF accounts here),
#   • the DeepSeek API key is OPTIONAL — the dashboard boots and works without
#     it; you only need it later to switch on AI auto-messaging (add it to .env).
#
# One-liner (paste into the Hostinger / VPS terminal — runs start to finish, no
# questions asked):
#   bash <(curl -fsSL https://raw.githubusercontent.com/BlackPhoenixSlo/inflowwkiller/main/scripts/deploy-here.sh)
#
# Already have a DeepSeek key? Pass it inline and it's baked in:
#   DEEPSEEK_API_KEY=sk-... bash <(curl -fsSL https://raw.githubusercontent.com/BlackPhoenixSlo/inflowwkiller/main/scripts/deploy-here.sh)
#
# Want a STABLE https URL instead of the random tunnel one? If this box already
# runs Traefik (every Hostinger *n8n* template does) and you point a DNS A-record
# at it, pass the host and it's routed straight through Traefik with a real
# Let's Encrypt cert — faster, permanent, no Cloudflare hop:
#   DOMAIN=app.yourdomain.com bash <(curl -fsSL https://raw.githubusercontent.com/BlackPhoenixSlo/inflowwkiller/main/scripts/deploy-here.sh)
#
# Re-run anytime to pull the latest code + rebuild (idempotent — keeps your
# .env, database, and sessions). Deploy your own fork by setting REPO_URL=.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/BlackPhoenixSlo/inflowwkiller.git}"
BRANCH="${BRANCH:-main}"
DIR="${DIR:-$HOME/inflowwkiller}"

say() { printf '\n\033[36m▸ %s\033[0m\n' "$*"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
gen_secret() { openssl rand -hex 32 2>/dev/null || head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# ---------- 1. Docker ----------
if ! command -v docker >/dev/null; then
  say "installing Docker"
  curl -fsSL https://get.docker.com | $SUDO sh
fi
docker compose version >/dev/null 2>&1 || die "docker compose plugin missing — install Docker Engine 20+ with the compose plugin"

# ---------- 2. git + clone/pull ----------
command -v git >/dev/null || { say "installing git"; ($SUDO apt-get update -y && $SUDO apt-get install -y git) 2>/dev/null || $SUDO yum install -y git; }
if [ -d "$DIR/.git" ]; then
  say "updating $DIR"
  git -C "$DIR" fetch origin; git -C "$DIR" checkout "$BRANCH"; git -C "$DIR" pull --ff-only
else
  say "cloning $REPO_URL into $DIR (branch: $BRANCH)"
  git clone --branch "$BRANCH" "$REPO_URL" "$DIR"
fi
cd "$DIR"

# ---------- 3. bind-mount targets ----------
[ -f service/proxies.json ] || echo '{"proxies":[]}' > service/proxies.json
[ -f service/chatterly.db ] || touch service/chatterly.db
mkdir -p service/sessions service/storyboard_cache

# ---------- 4. .env (idempotent — keep existing keys, mint session secrets once) ----------
# No secrets required: session secrets are generated, the share gate is off
# (friend-auth is the only gate), and the DeepSeek key is left blank unless you
# passed one. Nothing here blocks the boot.
say "writing .env (secrets stay on this box, chmod 600)"
touch .env; chmod 600 .env
upsert() { local k="$1" v="$2"; [ -z "$v" ] && return 0
  if grep -q "^$k=" .env; then sed -i "s|^$k=.*|$k=$v|" .env; else printf '%s=%s\n' "$k" "$v" >> .env; fi; }
grep -q '^SESSION_SECRET=.\+'         .env || upsert SESSION_SECRET "$(gen_secret)"
grep -q '^CHATTER_SESSION_SECRET=.\+' .env || upsert CHATTER_SESSION_SECRET "$(gen_secret)"
grep -q '^SHARE_TOKEN='               .env || echo 'SHARE_TOKEN=' >> .env   # empty = friend-auth only
# DeepSeek key is optional — only bake it in if you passed one inline. Otherwise
# leave a blank placeholder; add it later in .env when you want AI auto-messaging.
[ -n "${DEEPSEEK_API_KEY:-}" ] && upsert DEEPSEEK_API_KEY "$DEEPSEEK_API_KEY"
grep -q '^DEEPSEEK_API_KEY=' .env || echo 'DEEPSEEK_API_KEY=' >> .env
echo "  .env keys: $(grep -oE '^[A-Z_]+' .env | tr '\n' ' ')"
grep -q '^DEEPSEEK_API_KEY=.\+' .env || echo "  note: no DeepSeek key yet — dashboard works; AI auto-messaging stays off until you add DEEPSEEK_API_KEY to $DIR/.env"

# ---------- 5. choose exposure: stable Traefik route (preferred) or quick tunnel ----------
DOMAIN="${DOMAIN:-}"

# The base compose attaches `app` to the external `n8n_default` network so a
# host Traefik can reach it. On a box without an n8n stack that network is
# absent and `compose up` would refuse to start — create an empty stand-in so
# the bare-VPS tunnel path still boots. No-op if it already exists.
docker network inspect n8n_default >/dev/null 2>&1 || docker network create n8n_default >/dev/null 2>&1 || true

if [ -n "$DOMAIN" ]; then
  # Find the running Traefik, the docker network it's on, and its cert-resolver
  # name — so we adapt to the box instead of hard-coding template specifics.
  # Falls back to the Hostinger n8n template's defaults.
  TNAME=$(docker ps --format '{{.Names}} {{.Image}}' | awk 'tolower($2) ~ /traefik/ {print $1; exit}')
  [ -z "$TNAME" ] && die "DOMAIN set but no Traefik container is running on this box. Omit DOMAIN to use the Cloudflare tunnel, or start Traefik first."
  TNET=$(docker inspect "$TNAME" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' 2>/dev/null | grep -vE '^bridge$|^$' | head -1)
  TNET="${TNET:-n8n_default}"
  CR=$(docker inspect "$TNAME" --format '{{json .Args}}' 2>/dev/null | grep -oE 'certificatesresolvers\.[A-Za-z0-9_-]+' | head -1 | cut -d. -f2)
  CR="${CR:-mytlschallenge}"
  say "routing the dashboard through Traefik '$TNAME' on https://$DOMAIN (network=$TNET, certresolver=$CR)"
  # Auto-merged override: point the router at YOUR host + attach app to the
  # Traefik network. Regenerated every run (idempotent).
  cat > docker-compose.override.yml <<YAML
services:
  app:
    networks:
      - default
      - $TNET
    labels:
      traefik.enable: "true"
      traefik.docker.network: "$TNET"
      traefik.http.routers.chatterly.rule: "Host(\`$DOMAIN\`)"
      traefik.http.routers.chatterly.entrypoints: "websecure"
      traefik.http.routers.chatterly.tls.certresolver: "$CR"
      traefik.http.services.chatterly.loadbalancer.server.port: "3001"
networks:
  $TNET:
    external: true
YAML
  say "docker compose up -d --build (first build takes a few minutes)"
  docker compose up -d --build
  docker compose ps
  cat <<DONE

────────────────────────────────────────────────────────────
✓ Fastt is live behind Traefik with a real TLS certificate.

  URL:  https://${DOMAIN}
  dir:  ${DIR}   branch: ${BRANCH}

If the page doesn't load yet:
  • confirm ${DOMAIN} has a DNS A-record → this server's public IP
  • give Traefik ~30s to fetch the Let's Encrypt cert on first hit
  • check:  docker logs ${TNAME} 2>&1 | grep -iE "${DOMAIN}|certificate|error"

Connect an OnlyFans account: open the URL, then capture your session with the
Chrome extension in loginExtension/ (see DEPLOY.md, Step 4).

Re-run to update:   DOMAIN=${DOMAIN} bash $DIR/scripts/deploy-here.sh
────────────────────────────────────────────────────────────
DONE
  exit 0
fi

# ---------- 5b. fallback: Cloudflare quick tunnel ----------
# No DOMAIN → expose via a stateless Cloudflare tunnel. Disable the app's
# Traefik router so a host Traefik doesn't try (and fail) to serve the dead
# placeholder host from the base compose.
say "no DOMAIN given → Cloudflare quick tunnel.  (Stable, faster URL instead:  DOMAIN=app.yourdomain.com bash <(curl ...))"
cat > docker-compose.override.yml <<'YAML'
services:
  app:
    labels:
      traefik.enable: "false"
YAML
say "docker compose --profile tunnel up -d --build (first build takes a few minutes)"
docker compose --profile tunnel up -d --build
docker compose ps

# ---------- 6. public URL ----------
say "waiting for the Cloudflare tunnel URL"
URL=""
for _ in $(seq 1 30); do
  URL=$(docker logs chatterly-tunnel 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 || true)
  [ -n "$URL" ] && break; printf '.'; sleep 4
done; echo

if [ -z "$URL" ]; then
  echo "⚠ tunnel URL not seen yet. Check: docker logs -f chatterly-tunnel | grep trycloudflare"
  exit 0
fi
cat <<DONE

────────────────────────────────────────────────────────────
✓ Fastt is live on this server.

  URL:  ${URL}
  dir:  ${DIR}   branch: ${BRANCH}

Connect an OnlyFans account: open the URL, then capture your session with the
Chrome extension in loginExtension/ (see DEPLOY.md, Step 4).

Re-run to update:   bash $DIR/scripts/deploy-here.sh
Current URL anytime: docker logs chatterly-tunnel | grep trycloudflare | tail -1
For a permanent URL:  DOMAIN=app.yourdomain.com bash $DIR/scripts/deploy-here.sh
────────────────────────────────────────────────────────────
DONE
