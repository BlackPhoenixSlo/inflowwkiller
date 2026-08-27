#!/usr/bin/env bash
# notify.sh — push a short alert to whatever channels are configured.
#
#   notify.sh <severity> <title> [body]
#     severity: crit | warn | ok
#
# Channels are opt-in via env (set them in the config file, NEVER here — this
# file is public-repo-synced and must stay credential-free):
#
#   WHATSAPP_PHONE + WHATSAPP_APIKEY   CallMeBot. Zero infra: message their
#                                      number once from your WhatsApp to get a
#                                      key. Phone in full international form,
#                                      no +, e.g. 38641234567.
#   ALERT_WEBHOOK_URL                  POSTs {severity,title,body,host,ts} as
#                                      JSON. This is the n8n path — point it at
#                                      an n8n Webhook node and let n8n fan out
#                                      to WhatsApp/anything else.
#   TELEGRAM_TOKEN + TELEGRAM_CHAT_ID  Reuses the bot infra already on the box.
#
# Every channel is best-effort and independently timed out: an alert path that
# hangs must never wedge the watchdog that called it, and a box in trouble is
# exactly when an outbound HTTP call is most likely to stall.
set -uo pipefail

SEV="${1:?severity required}"; TITLE="${2:?title required}"; BODY="${3:-}"
CFG="${WATCHDOG_CONFIG:-/root/fastt/watchdog.env}"
# shellcheck disable=SC1090
[ -f "$CFG" ] && . "$CFG"

HOST="$(hostname -s 2>/dev/null || echo host)"
TS="$(date -u '+%Y-%m-%d %H:%M UTC')"
case "$SEV" in
  crit) ICON="🚨" ;;
  warn) ICON="⚠️" ;;
  ok)   ICON="✅" ;;
  *)    ICON="•"  ;;
esac
TEXT="$ICON $TITLE"
[ -n "$BODY" ] && TEXT="$TEXT
$BODY"
TEXT="$TEXT
— $HOST $TS"

sent=0

if [ -n "${WHATSAPP_PHONE:-}" ] && [ -n "${WHATSAPP_APIKEY:-}" ]; then
  # CallMeBot takes the message as a query param, so it must be URL-encoded.
  # --data-urlencode with -G does that without us hand-rolling an encoder.
  curl -sS -G --max-time 20 \
    --data-urlencode "phone=${WHATSAPP_PHONE}" \
    --data-urlencode "text=${TEXT}" \
    --data-urlencode "apikey=${WHATSAPP_APIKEY}" \
    "https://api.callmebot.com/whatsapp.php" >/dev/null 2>&1 && sent=1
fi

if [ -n "${ALERT_WEBHOOK_URL:-}" ]; then
  # jq builds the JSON so a message containing a quote or newline cannot break
  # the payload (hand-rolled string interpolation here is a real footgun —
  # container names and error text routinely contain both).
  if command -v jq >/dev/null 2>&1; then
    PAYLOAD="$(jq -nc --arg s "$SEV" --arg t "$TITLE" --arg b "$BODY" \
                     --arg h "$HOST" --arg ts "$TS" \
               '{severity:$s,title:$t,body:$b,host:$h,ts:$ts}')"
    curl -sS --max-time 20 -H 'Content-Type: application/json' \
      -d "$PAYLOAD" "$ALERT_WEBHOOK_URL" >/dev/null 2>&1 && sent=1
  fi
fi

if [ -n "${TELEGRAM_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  curl -sS --max-time 20 \
    -d "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=${TEXT}" \
    "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" >/dev/null 2>&1 && sent=1
fi

# Always leave a local trail, so an alert is recoverable even when every
# channel is down (which is the failure mode that matters most).
LOG="${WATCHDOG_LOG:-/var/log/fastt-watchdog.log}"
printf '%s [%s] %s %s\n' "$TS" "$SEV" "$TITLE" "${BODY//$'\n'/ | }" >> "$LOG" 2>/dev/null || true

[ "$sent" -eq 1 ] || exit 3   # 3 = nothing configured / all channels failed
