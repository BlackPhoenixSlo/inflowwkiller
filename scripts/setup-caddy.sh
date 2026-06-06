#!/usr/bin/env bash
#
# setup-caddy.sh — replace the cloudflared quick tunnel with a real
# Caddy + Let's Encrypt HTTPS reverse proxy on the VPS.
#
# Usage:
#   ./scripts/setup-caddy.sh root@<vps-ip> <domain>
#
# Example:
#   ./scripts/setup-caddy.sh root@YOUR_VPS_IP chatterly.example.com
#
# What it does:
#   1. Verifies the domain's A record points at the VPS IP (loops every
#      10s for up to 5 min if DNS hasn't propagated yet).
#   2. Installs Caddy from its official apt repo (idempotent).
#   3. Writes /etc/caddy/Caddyfile reverse-proxying <domain> → 127.0.0.1:3001
#      with WebSocket support, gzip, sensible upload size cap, and a
#      proper X-Forwarded-* header set so the relay's audit log sees the
#      real client IP instead of "127.0.0.1".
#   4. Opens TCP 80 (cert challenge) + 443 (HTTPS) in UFW if active;
#      no-ops if UFW isn't installed (Hostinger's default).
#   5. Restarts caddy. Let's Encrypt issues a cert on first boot (~30s).
#   6. Stops + disables the docker `tunnel` profile so cloudflared stops
#      eating bandwidth.
#   7. Prints the new HTTPS URL with the SHARE_TOKEN.
#
# Reversible: `docker compose --profile tunnel up -d` brings the
# cloudflared tunnel back; `systemctl stop caddy` shuts off the new path.

set -euo pipefail

usage() {
  cat <<EOF
Usage: $0 <ssh-target> <domain> [--port N]
  ssh-target  e.g. root@YOUR_VPS_IP
  domain      the FQDN already pointing at the VPS (A record)
  --port N    SSH port if non-default
EOF
  exit 1
}

[[ $# -ge 2 ]] || usage
case "$1" in -h|--help) usage ;; esac

SSH_TARGET="$1"; DOMAIN="$2"; shift 2
SSH_PORT=22
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) SSH_PORT="$2"; shift 2 ;;
    *) echo "unknown flag: $1" >&2; usage ;;
  esac
done

SSH=(ssh -p "$SSH_PORT" -o StrictHostKeyChecking=accept-new "$SSH_TARGET")

say() { printf '\n\033[36m▸ %s\033[0m\n' "$*"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# Get the VPS's public IP so we can sanity-check the A record.
VPS_IP=$("${SSH[@]}" 'curl -sS -4 https://ipinfo.io/ip')
[[ -n "$VPS_IP" ]] || die "couldn't determine VPS public IP"
say "VPS public IP: $VPS_IP — checking that $DOMAIN resolves to it"

for attempt in $(seq 1 30); do
  RESOLVED=$(dig +short "$DOMAIN" | tail -n 1 || true)
  if [[ "$RESOLVED" == "$VPS_IP" ]]; then
    echo "  $DOMAIN → $RESOLVED ✓"
    break
  fi
  echo "  attempt $attempt/30 — $DOMAIN currently resolves to: ${RESOLVED:-<nothing>}  (want: $VPS_IP)"
  [[ $attempt -eq 30 ]] && die "DNS hasn't propagated after 5 minutes. Verify the A record at your registrar."
  sleep 10
done

say "installing Caddy on the VPS (apt repo, idempotent)"
"${SSH[@]}" bash -s <<'REMOTE'
set -euo pipefail
if ! command -v caddy >/dev/null; then
  apt-get update -y
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi
caddy version
REMOTE

say "writing /etc/caddy/Caddyfile (reverse proxy to 127.0.0.1:3001)"
"${SSH[@]}" "cat > /etc/caddy/Caddyfile" <<EOF
{
    # Let's Encrypt staging would go here. Leaving blank uses prod.
    email admin@$DOMAIN
}

$DOMAIN {
    # Reverse proxy everything to the Next.js container.
    # WebSocket frames (/ws/events, /events SSE) are forwarded
    # automatically — Caddy detects the Upgrade header and switches
    # to bidirectional streaming without needing per-route config.
    reverse_proxy 127.0.0.1:3001 {
        # Long-lived event streams must not be cut off by the default
        # 30s read timeout. 24h is generous; the connection will be
        # closed when the user navigates away.
        transport http {
            read_timeout 24h
            response_header_timeout 24h
        }
        # Surface the real client IP to the relay so its audit log
        # records something useful instead of "127.0.0.1".
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }

    # Image responses are immutable — Caddy adds the right Cache-Control
    # if upstream omits it (the relay sets it for /img/*, so this is
    # belt-and-braces).
    header /img/* {
        Cache-Control "public, max-age=172800, immutable"
    }

    # Vault uploads can be large. Bump the per-request body limit
    # well above the relay's own cap so users don't hit a Caddy 413
    # before reaching the relay's validation.
    request_body {
        max_size 200MB
    }

    encode gzip

    # JSON access log to stdout — `journalctl -u caddy -f` to follow.
    log {
        output stdout
        format json
    }
}
EOF

say "opening firewall ports 80 + 443 (if UFW is active)"
"${SSH[@]}" bash -s <<'REMOTE'
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp
  ufw allow 443/tcp
  echo "  ufw rules added"
else
  echo "  ufw not active — relying on Hostinger's default open inbound"
fi
REMOTE

say "validating Caddyfile + restarting caddy (Let's Encrypt issues a cert on first boot, ~30s)"
"${SSH[@]}" bash -s <<'REMOTE'
set -euo pipefail
caddy validate --config /etc/caddy/Caddyfile
systemctl enable caddy
systemctl restart caddy
sleep 3
systemctl is-active caddy
REMOTE

say "waiting for Let's Encrypt cert (up to 90s)"
CERT_OK=0
for attempt in $(seq 1 18); do
  if "${SSH[@]}" "curl -sS -o /dev/null -w '%{http_code}' --max-time 10 https://$DOMAIN/health" | grep -qE '^(200|401)$'; then
    CERT_OK=1
    break
  fi
  echo "  attempt $attempt — cert not ready yet"
  sleep 5
done
[[ $CERT_OK -eq 1 ]] || die "Caddy didn't serve a valid cert after 90s. Run: ssh $SSH_TARGET journalctl -u caddy -n 50"

say "stopping the cloudflared tunnel — no longer needed"
"${SSH[@]}" bash -s <<REMOTE
set -euo pipefail
cd ~/chatterly
docker compose --profile tunnel stop tunnel || true
docker compose --profile tunnel rm -f tunnel || true
REMOTE

SHARE_TOKEN=$("${SSH[@]}" "grep SHARE_TOKEN ~/chatterly/.env | cut -d= -f2-")

cat <<DONE

────────────────────────────────────────────────────────────
✓ Caddy is up. Tunnel is down.

  Your stable URL:
    https://$DOMAIN/?t=$SHARE_TOKEN

  Once you hit it once, the share-token cookie is set for 7 days
  and you can drop the ?t=... on subsequent visits.

  Caddy logs:        ssh $SSH_TARGET 'journalctl -u caddy -f'
  Caddy reload:      ssh $SSH_TARGET 'systemctl reload caddy'
  Bring tunnel back: ssh $SSH_TARGET 'cd ~/chatterly && docker compose --profile tunnel up -d tunnel'
────────────────────────────────────────────────────────────
DONE
