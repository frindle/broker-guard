#!/usr/bin/env bash
# One-shot update for the broker-guard container.
# Pulls main, rebuilds the image when something in it changed, and restarts
# the service.
#
# Usage:  ./update.sh
#         ./update.sh --force    # build even if nothing in the image changed
#
# The image is rebuilt only when a file that goes into it changed since the
# last successful build; `docker compose up -d` always runs. (The Dockerfile
# clones eraser from upstream at build time; a skipped build does not refresh
# it. Bump ERASER_REF in the Dockerfile, or use --force.)
#
# Safe to run any time: everything persistent (profile, settings.json,
# state.sqlite, review trail, logs) lives on the gitignored ./state and ./logs
# volumes, which a hard reset never touches. Settings saved on the web UI's
# /settings page live in /data/settings.json and survive too. Machine-specific
# config belongs in the gitignored .env / docker-compose.override.yml, never in
# tracked files (the reset below would wipe it).

set -e

cd "$(dirname "$0")"

FORCE_BUILD=""
case "${1:-}" in
  --force) FORCE_BUILD="--force" ;;
esac

# --- build only what changed -------------------------------------------------
# $BUILD_MARKER holds the commit the image was last built from. It is
# gitignored, so `git reset --hard` leaves it alone. It is written only after a
# build succeeded; a skipped run leaves it, so the next diff still starts at the
# last real build.
BUILD_MARKER=.last-built-sha

# is_build_input <path>: succeeds when a change to <path> can change the image.
# This is the list of what the Dockerfile COPYs (plus the Dockerfile, compose
# file and .dockerignore). Anything not listed never reaches the image.
is_build_input() {
  case "$1" in
    Dockerfile|docker-compose.yml|.dockerignore|requirements.txt) return 0 ;;
    docker-entrypoint.sh|profile.example.json|data/source-brokers.json) return 0 ;;
    tools/reprobe_blocked.py|broker_guard/*) return 0 ;;
  esac
  return 1
}

# decide_build: sets DO_BUILD (1 or 0) and BUILD_WHY. When in doubt: build.
decide_build() {
  DO_BUILD=1
  local new old changed f n=0
  new="$(git rev-parse HEAD)"
  old=""
  if [ -r "$BUILD_MARKER" ]; then old="$(tr -d '[:space:]' < "$BUILD_MARKER" 2>/dev/null || true)"; fi
  if [ -n "$FORCE_BUILD" ]; then BUILD_WHY="$FORCE_BUILD given"; return; fi
  if [ -z "$old" ]; then BUILD_WHY="no build marker yet (first run)"; return; fi
  if ! git cat-file -e "${old}^{commit}" 2>/dev/null; then BUILD_WHY="build marker is not a known commit"; return; fi
  if [ "$old" = "$new" ]; then DO_BUILD=0; BUILD_WHY="no new commits since the last build (${new:0:7})"; return; fi
  if ! changed="$(git diff --relative --no-renames --name-only "$old" "$new" -- 2>/dev/null)"; then
    BUILD_WHY="cannot diff ${old:0:7}..${new:0:7}"; return
  fi
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if is_build_input "$f"; then BUILD_WHY="image input changed: $f"; return; fi
    n=$((n + 1))
  done <<< "$changed"
  DO_BUILD=0
  BUILD_WHY="${n} file(s) changed since ${old:0:7}, none of them go into the image"
}

# Hard-reset to origin's main instead of `git pull`: this deploy clone has no
# local commits, and fetch+reset always lands exactly on origin/main even if
# upstream history was rewritten.
echo "=== fetching + hard reset to origin/main ==="
git fetch origin
git reset --hard origin/main
echo "now at $(git log --oneline -1)"

decide_build
if [ "$DO_BUILD" = 1 ]; then
  echo "=== building: $BUILD_WHY ==="
  docker compose build broker-guard
  # Reached only when the build succeeded (set -e).
  git rev-parse HEAD > "$BUILD_MARKER"
else
  echo "=== build skipped: $BUILD_WHY ==="
fi

echo "=== restart ==="
docker compose up -d --remove-orphans broker-guard

echo "=== done ==="
docker compose ps broker-guard
# The healthcheck reads this file; "running" means a scan is in progress.
if [ -f logs/heartbeat.json ]; then
  echo "heartbeat: $(cat logs/heartbeat.json)"
fi
