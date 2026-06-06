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
# What is NOT included: anything that drives a real browser (Playwright,
# capture scripts, dev `--reload` watcher). Auth state arrives via the
# loginExtension paste-curl flow once the container is running.

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

# Copy the whole service package in one shot. This repo is already curated:
# dev/live-test drivers (_drive_*/_test_*/_send_*), capture scripts, the test
# suite, secrets (.session_secret/.chatter_secret) and *.db files are all
# excluded from the tree and from .dockerignore. A single COPY avoids the
# brittle per-file allowlist that used to crash boot at runtime whenever a new
# module landed (the whole automations engine — automation_executor,
# webhook_dispatch, funnels_api, the automations/ package — needs to be here).
COPY service/ service/
COPY web/ web/

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
CMD ["uvicorn", "service.server:app", "--host", "0.0.0.0", "--port", "8787"]
