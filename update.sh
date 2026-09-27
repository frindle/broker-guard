#!/usr/bin/env bash
# One-shot update for the broker-guard container.
# Pulls main, rebuilds the image and restarts the service.
#
# Usage:  ./update.sh
#
# Safe to run any time: everything persistent (profile, settings.json,
# state.sqlite, review trail, logs) lives on the gitignored ./state and ./logs
# volumes, which a hard reset never touches. Settings saved on the web UI's
# /settings page live in /data/settings.json and survive too. Machine-specific
# config belongs in the gitignored .env / docker-compose.override.yml, never in
# tracked files (the reset below would wipe it).

set -e

cd "$(dirname "$0")"

# Hard-reset to origin's main instead of `git pull`: this deploy clone has no
# local commits, and fetch+reset always lands exactly on origin/main even if
# upstream history was rewritten.
echo "=== fetching + hard reset to origin/main ==="
git fetch origin
git reset --hard origin/main
echo "now at $(git log --oneline -1)"

echo "=== building ==="
docker compose build broker-guard

echo "=== restart ==="
docker compose up -d --remove-orphans broker-guard

echo "=== done ==="
docker compose ps broker-guard
# The healthcheck reads this file; "running" means a scan is in progress.
if [ -f logs/heartbeat.json ]; then
  echo "heartbeat: $(cat logs/heartbeat.json)"
fi
