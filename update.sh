#!/usr/bin/env bash
# One-shot update for the broker-guard container.
# Pulls main, rebuilds the image only when something in it changed, restarts the service.
#
# Usage:  ./update.sh
#         ./update.sh --no-cache    # rebuild from scratch (slower; use when
#                                     a dependency changed and you suspect
#                                     a stale layer)
#         ./update.sh --force       # build even if nothing in the image changed
#
# The image is rebuilt only when a file that goes into it changed since the
# last successful build. `up -d` always runs, so .env / compose edits are still
# picked up. (The eraser stage clones a pinned tag; a skipped build does not refresh it. Use --force.)

set -e
cd "$(dirname "$0")"

EXTRA_BUILD_ARGS=""
FORCE_BUILD=""
case "${1:-}" in
  --no-cache) EXTRA_BUILD_ARGS="--no-cache"; FORCE_BUILD="--no-cache" ;;
  --force) FORCE_BUILD="--force" ;;
esac

# Compose: the v2 plugin (`docker compose`) when present, else legacy docker-compose.
# BuildKit is switched on for both (same cache as `docker build`; v2 always uses it,
# COMPOSE_DOCKER_CLI_BUILD makes legacy docker-compose hand the build to it).
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
else
  DC="docker-compose"
fi
export DOCKER_BUILDKIT=1 COMPOSE_DOCKER_CLI_BUILD=1

# --- build only what changed -------------------------------------------------
# $BUILD_MARKER holds the commit the image was last built from. It is
# gitignored, so `git reset --hard` leaves it alone. It is written only after a
# build succeeded; a skipped run leaves it, so the next diff still starts at the
# last real build.
BUILD_MARKER=.last-built-sha

# is_build_input <path>: succeeds when a change to <path> can change the image.
# The Dockerfile copies broker_guard/, tools/reprobe_blocked.py, profile.example.json,
# docker-entrypoint.sh, data/source-brokers.json and requirements.txt (and clones
# eraser at a pinned tag), so only those paths and the build files count.
# Unknown paths build.
is_build_input() {
  case "$1" in
    # Always inputs, and runtime directories (a *.md or test there still ships).
    Dockerfile|docker-compose.yml|.dockerignore|requirements.txt|docker-entrypoint.sh|profile.example.json) return 0 ;;
    broker_guard/*|data/source-brokers.json|tools/reprobe_blocked.py) return 0 ;;
    # Never in the image: the Dockerfile copies only the paths above.
    *.md|tests/*|tools/*|data/*|gen_*.py|update.sh|.github/*|.gitignore|.gitattributes) return 1 ;;
  esac
  return 0
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

# Hard-reset to origin's main instead of `git pull`: this deploy clone has
# no local commits, and a plain pull fails with "divergent branches" whenever
# upstream history is force-pushed (e.g. a history cleanup). fetch+reset is
# deterministic and always lands exactly on origin/main.
echo "=== fetching + hard reset to origin/main ==="
git fetch origin
git reset --hard origin/main

echo "=== last commit ==="
git --no-pager log -1 --pretty='%h %s'

decide_build
if [ "$DO_BUILD" = 1 ]; then
  echo "=== building${EXTRA_BUILD_ARGS:+ (no cache)}: $BUILD_WHY ==="
  # shellcheck disable=SC2086
  $DC build $EXTRA_BUILD_ARGS broker-guard
  # Reached only when the build succeeded (set -e).
  git rev-parse HEAD > "$BUILD_MARKER"
else
  echo "=== build skipped: $BUILD_WHY ==="
fi

echo "=== restart ==="
$DC up -d --remove-orphans broker-guard

echo "=== done ==="
$DC ps broker-guard
# The healthcheck reads this file; "running" means a scan is in progress.
if [ -f logs/heartbeat.json ]; then
  echo "heartbeat: $(cat logs/heartbeat.json)"
fi
