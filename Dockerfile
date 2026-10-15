# syntax=docker/dockerfile:1.6
#
# OF Relay — runtime image (Python).
#
# Mirrors what `./scripts/start.sh` boots natively as `venv/bin/uvicorn
# service.server:app`, but inside a container so the same artefact runs on
# your laptop, on a $5/mo VPS (Hetzner / DO / Vultr), or on Railway/Fly.
#
# Companion image: app/Dockerfile builds the Next.js /inbox UI.
# Wired together in docker-compose.yml at the repo root.
#
# Includes Playwright Chromium + Xvfb: the remote-browser login launches
# Chromium headed (headless=False, fewer Cloudflare challenges), so CMD starts
# an Xvfb virtual display (DISPLAY=:99) before uvicorn. NOT included: capture
# scripts and the dev `--reload` watcher.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# curl_cffi ships pre-built libcurl-impersonate wheels for linux/amd64 +
# linux/arm64, so no compiler toolchain is needed. ca-certificates is for
# OF + proxy TLS verification. curl backs the HEALTHCHECK below. ffmpeg
# (+ ffprobe) is used by the vault-thumbnail extractor in server.py to
# pull 12 frames out of source mp4s when the UI's VaultPicker hovers a
# video tile.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first so the layer caches across code edits.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Chromium for the remote-browser login (playwright comes in via
# requirements.txt) plus Xvfb so the headed launch has a display.
# tini is PID 1: it reaps Xvfb/Chromium children and forwards SIGTERM to uvicorn.
RUN python -m playwright install --with-deps chromium \
 && apt-get update && apt-get install -y --no-install-recommends xvfb tini \
 && rm -rf /var/lib/apt/lists/*

# Copy the whole service package in one shot. The synced tree is already
# curated: dev/live-test drivers (_drive_*/_test_*/_send_*), capture scripts,
# the test suite, secrets (.session_secret/.chatter_secret) and *.db files are
# all excluded by the deploy filter and .dockerignore.
#
# ⚠️ DO NOT go back to a per-file allowlist. One lived here and crashed boot
# every time a new module landed; the deploy repo replaced it with this single
# COPY, that fix was never back-ported, and on 2026-08-21 the first commit to
# touch this file synced the allowlist back over it — server.py imports 32
# modules it did not list, so the relay died on `ModuleNotFoundError:
# account_health` and took fastt-app down with it. The whole automations engine
# (automation_executor, webhook_dispatch, funnels_api, the automations/ package)
# has to be in the image, and an allowlist cannot keep up with that by hand.
COPY service/ service/
# The Fansly platform package. fansly_backend (imported at boot by
# transaction_ingest and server) puts ../fansly on sys.path and imports
# fansly_client from it; without this COPY the relay dies on ModuleNotFoundError.
# Only the runtime modules are in the repo; captures, HARs and sessions never are.
COPY fansly/ fansly/
COPY web/ web/
# The Infloww-skin frontend. server.py mounts it at /infloww from
# HERE.parent/"ui-redesign"/"infloww-exact" — i.e. /app/ui-redesign/… — and the
# mount is guarded by `if _INFLOWW_DIR.is_dir()`. Without this COPY the folder is
# absent in the image, that guard is False, and the whole 54-page skin 404s while
# the relay boots perfectly healthy. It rsyncs to the VPS but never entered the
# image; that gap made /infloww dark in prod until 2026-08-21.
COPY ui-redesign/ ui-redesign/

# Persistent state lives outside the image so a rebuild never wipes your
# auth or your event history. Volume targets:
#   /app/service/sessions   — captured OF sessions (loginExtension output)
#   /app/service/proxies.json — proxy registry
#   /app/service/chatterly.db — SQLite event store (WAL + SHM created beside it)
RUN mkdir -p /app/service/sessions

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8787/livez > /dev/null || exit 1

# Single worker on purpose — SQLite write lock + the WebSocket pump is a
# module-level singleton. Production mode, no --reload.
# Xvfb gives the headed Chromium a display; the stale lock is removed so a
# `docker restart` (same /tmp) doesn't leave Xvfb refusing display :99.
ENV DISPLAY=:99
ENTRYPOINT ["tini", "--"]
CMD ["sh", "-c", "rm -f /tmp/.X99-lock; Xvfb :99 -screen 0 1440x900x24 -nolisten tcp & exec uvicorn service.server:app --host 0.0.0.0 --port 8787"]
