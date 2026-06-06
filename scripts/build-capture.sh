#!/usr/bin/env bash
# Build the capture sidecar image FOR THE CURRENT HOST ARCH.
#
# Why this exists: Docker silently reuses cached base images regardless of
# architecture. If you ever pulled `mcr.microsoft.com/playwright/python` as
# amd64 (a prior build, a docker pull, anything), every subsequent build
# inherits that amd64 layer and runs under QEMU emulation — ~20× slower.
# This script forces a fresh pull of the correct arch + rebuilds.
#
# Usage:
#   ./scripts/build-capture.sh           # build + start the sidecar
#   ./scripts/build-capture.sh --no-up   # build only, don't start
#
# After this finishes once, normal `docker compose --profile capture up -d
# capture` is enough for restarts — the layers are cached correctly.

set -euo pipefail

cd "$(dirname "$0")/.."

# Detect host arch
ARCH=$(uname -m)
case "$ARCH" in
  arm64|aarch64) PLATFORM=linux/arm64 ;;
  x86_64|amd64)  PLATFORM=linux/amd64 ;;
  *)             PLATFORM=linux/$ARCH ;;
esac
echo "[build-capture] host arch: $ARCH → target: $PLATFORM"

# Cancel any in-flight build of the capture container so the next steps
# aren't competing for Docker resources.
docker compose --profile capture stop capture 2>/dev/null || true

# Force buildx and the daemon to default to the host's arch. Both env vars
# are needed because compose hands off to buildkit, which doesn't always
# inherit the daemon-level platform default.
export DOCKER_DEFAULT_PLATFORM=$PLATFORM
export BUILDX_PLATFORMS=$PLATFORM

# Re-pull the base image at the right arch. There are TWO caches at play:
#   1. `docker images` — the local image store (per-platform).
#   2. BuildKit's content-addressable cache — separate, per-layer.
# A multi-arch manifest list has one digest covering BOTH arches, so Docker
# can decide "have it" even though only the wrong variant is locally present.
# We nuke both caches to be certain.
BASE=mcr.microsoft.com/playwright/python:v1.59.0-jammy
WANT_ARCH=$(echo "$PLATFORM" | sed 's|linux/||')

# Cache 1: docker images
if docker image inspect "$BASE" >/dev/null 2>&1; then
  CURRENT_ARCH=$(docker image inspect "$BASE" --format '{{.Architecture}}')
  if [[ "$CURRENT_ARCH" != "$WANT_ARCH" ]]; then
    echo "[build-capture] cache 1 has $CURRENT_ARCH but we need $WANT_ARCH — removing"
    docker image rm "$BASE" || true
  else
    echo "[build-capture] cache 1: $CURRENT_ARCH ✓"
  fi
fi

# Cache 2: BuildKit. There's no targeted "remove this base from buildkit
# cache" — the cleanest reliable option is a full prune of build-only data
# (does NOT touch your running containers or images you've pulled for other
# projects). Recoverable: subsequent builds re-download what they need.
echo "[build-capture] flushing BuildKit cache (forces a fresh native pull)"
docker builder prune -af >/dev/null 2>&1 || docker buildx prune -af >/dev/null 2>&1 || true

# Cache 3: explicit pre-pull at the right platform. Now that both caches
# are clear, this puts the correct-arch base in cache 1 BEFORE buildkit
# starts resolving FROM lines, so it can't grab the wrong one.
echo "[build-capture] pre-pulling $BASE for $PLATFORM (this is the big download)"
docker pull --platform "$PLATFORM" "$BASE"

echo "[build-capture] building of-relay-capture for $PLATFORM (apt step should take ~60-90s native)"
docker compose --profile capture build --no-cache capture

WANT_UP=true
for arg in "$@"; do
  case "$arg" in
    --no-up) WANT_UP=false ;;
    *) echo "[build-capture] unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if [[ "$WANT_UP" == "true" ]]; then
  echo "[build-capture] starting capture sidecar"
  docker compose --profile capture up -d capture
  # Wait up to 30s for /health to answer (Xvfb + x11vnc + websockify + uvicorn
  # take ~10s to all come up the first time).
  for i in $(seq 1 30); do
    if curl -fsS -o /dev/null --max-time 1 http://127.0.0.1:6081/health 2>/dev/null; then
      echo "[build-capture] sidecar ready ✓"
      echo
      echo "  Now refresh the relay UI — '🌐 Login on the server' will work from any device."
      exit 0
    fi
    sleep 1
  done
  echo "[build-capture] sidecar didn't answer in 30s — check logs:"
  echo "    docker compose --profile capture logs --tail 60 capture"
fi
