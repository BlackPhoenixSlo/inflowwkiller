#!/usr/bin/env bash
#
# deploy-here.sh — run THIS ON the VPS itself (e.g. Hostinger's browser terminal).
# No laptop, no SSH. It installs Docker, clones/updates the repo, brings the
# stack up, and prints how to reach it. By default that's plain http on this
# server's own IP — zero hassle, no domain, no DNS. Pass DOMAIN= to get a real
# https url instead (see the modes below).
#
# Zero secrets, zero proxies needed for a brand-new account:
#   • session secrets are generated automatically on first run,
#   • no proxy is required for one account (it uses this server's own IP —
#     proxies only matter once you host MULTIPLE OF accounts here),
#   • the DeepSeek API key is OPTIONAL — the dashboard boots and works without
#     it. Mind WHERE it goes, though: the moment you sign in and capture an OF
#     account, that account is OWNED by your login, and llm_client bills an
#     owned account to the key its owner pasted at Setup → Your AI keys. The
#     .env key below is the HOUSE key and is spent only on accounts nobody
#     owns — so .env alone does NOT switch AI auto-messaging on.
#
# One-liner (paste into the Hostinger / VPS terminal — runs start to finish, no
# questions asked):
#   bash <(curl -fsSL https://raw.githubusercontent.com/BlackPhoenixSlo/inflowwkiller/main/scripts/deploy-here.sh)
#
# Modes (same one-liner, just prefix an env var). Re-run anytime to switch — it's
# idempotent and keeps your .env, database, and sessions:
#
#   (default)                      → plain http on this server's IP   http://<ip>:3000
#   PORT=8080  ...                 → same, on a different host port
#   DOMAIN=you.duckdns.org ...     → real https via Traefik (free DuckDNS subdomain)
#   DOMAIN=app.yourdomain.com ...  → real https via Traefik (your own domain)
#   TUNNEL=1  ...                  → throwaway https url, no DNS (random, changes on restart)
#   DEEPSEEK_API_KEY=sk-... ...    → bake in the HOUSE AI key (optional; owned accounts
#                                    read Setup → Your AI keys instead — see above)
#
# A DOMAIN needs Traefik already on the box — every Hostinger *n8n* template
# ships it. Deploy your own fork by setting REPO_URL=.

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
# leave a blank placeholder. It is the HOUSE key (ownerless accounts only); the
# account you capture is owned by your login and reads Setup → Your AI keys.
[ -n "${DEEPSEEK_API_KEY:-}" ] && upsert DEEPSEEK_API_KEY "$DEEPSEEK_API_KEY"
grep -q '^DEEPSEEK_API_KEY=' .env || echo 'DEEPSEEK_API_KEY=' >> .env
echo "  .env keys: $(grep -oE '^[A-Z_]+' .env | tr '\n' ' ')"
grep -q '^DEEPSEEK_API_KEY=.\+' .env || echo "  note: no DeepSeek key yet — the dashboard works without one. Switch AI on at Setup → Your AI keys once your account is captured (this .env key covers only accounts with no owner)."

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

# ---------- 5b. opt-in: Cloudflare quick tunnel (TUNNEL=1) ----------
# Throwaway https url, no DNS — handy for a quick demo. Disable the app's
# Traefik router so a host Traefik doesn't also try to serve the placeholder
# host from the base compose.
if [ "${TUNNEL:-}" = "1" ]; then
  say "TUNNEL=1 → Cloudflare quick tunnel (random https url, changes on every restart)"
  cat > docker-compose.override.yml <<'YAML'
services:
  app:
    labels:
      traefik.enable: "false"
YAML
  say "docker compose --profile tunnel up -d --build (first build takes a few minutes)"
  docker compose --profile tunnel up -d --build
  docker compose ps
  say "waiting for the Cloudflare tunnel URL"
  URL=""
  for _ in $(seq 1 30); do
    URL=$(docker logs chatterly-tunnel 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 || true)
    [ -n "$URL" ] && break; printf '.'; sleep 4
  done; echo
  [ -z "$URL" ] && { echo "⚠ tunnel URL not seen yet. Check: docker logs -f chatterly-tunnel | grep trycloudflare"; exit 0; }
  cat <<DONE

────────────────────────────────────────────────────────────
✓ Fastt is live (Cloudflare tunnel).

  URL:  ${URL}   ← random, changes every restart
  dir:  ${DIR}   branch: ${BRANCH}

Current URL anytime: docker logs chatterly-tunnel | grep trycloudflare | tail -1
Want a STABLE url?    DOMAIN=you.duckdns.org bash $DIR/scripts/deploy-here.sh
────────────────────────────────────────────────────────────
DONE
  exit 0
fi

# ---------- 5c. default: plain http on this server's IP (zero hassle) ----------
# The simplest path that just works: publish the dashboard on a host port and
# print http://<this-server-ip>:<port>. No DNS, no domain, stable, instant.
# Trade-off: NO TLS — the login/session token travels in clear text, so treat
# this as quick-start and upgrade to https (footer) once it's working.
PORT="${PORT:-3000}"
IP=$(curl -fsS https://api.ipify.org 2>/dev/null || curl -fsS https://ifconfig.me 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')
IP="${IP:-<this-server-ip>}"
say "exposing the dashboard on http://$IP:$PORT (plain http — see footer to add https)"
# Disable the placeholder Traefik router and publish the port. (compose merges
# this with the base 127.0.0.1 bind, adding a public one.)
cat > docker-compose.override.yml <<YAML
services:
  app:
    ports:
      - "0.0.0.0:$PORT:3001"
    labels:
      traefik.enable: "false"
YAML
say "docker compose up -d --build (first build takes a few minutes)"
docker compose up -d --build
docker compose ps
cat <<DONE

────────────────────────────────────────────────────────────
✓ Fastt is live on this server.

  URL:  http://$IP:$PORT
  dir:  $DIR   branch: $BRANCH

  ⚠ plain http (no encryption). If the page won't open, allow the port in your
    firewall:   ufw allow $PORT/tcp    (or your VPS provider's firewall panel)

Connect an OnlyFans account: open the URL, then capture your session with the
Chrome extension in loginExtension/ (see DEPLOY.md, Step 4).

── upgrade to a secure, permanent https url (free or cheap) ─────────────────
  1) Free DuckDNS subdomain (recommended): create one at https://www.duckdns.org,
     point it at $IP, then re-run:
        DOMAIN=yourname.duckdns.org bash $DIR/scripts/deploy-here.sh
  2) Your own domain: add a DNS A-record → $IP, then re-run:
        DOMAIN=app.yourdomain.com bash $DIR/scripts/deploy-here.sh
  (both route through the box's Traefik — every Hostinger n8n template has it.)
────────────────────────────────────────────────────────────
DONE
