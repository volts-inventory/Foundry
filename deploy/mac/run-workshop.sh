#!/usr/bin/env bash
# Launch wrapper for the Foundry Workshop on macOS (M-series).
#
# launchd doesn't read an EnvironmentFile, so this wrapper sources foundry.env
# itself, then runs the server under `caffeinate` so a days-long build is never
# interrupted by the Mac going to sleep.
#
#   -i  prevent IDLE sleep      -s  prevent sleep on AC power
#   (Closing a laptop lid still sleeps regardless — keep the lid open, or run
#    clamshell on AC with an external display, for multi-day runs.)
set -euo pipefail

# repo root = two levels up from this script (deploy/mac/ -> repo)
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="${FOUNDRY_ENV_FILE:-$HOME/.config/foundry/foundry.env}"

if [ -f "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
else
  echo "warning: env file not found at $ENV_FILE (using process env only)" >&2
fi

cd "$REPO_ROOT"
exec caffeinate -is uv run python -m webapp.server
