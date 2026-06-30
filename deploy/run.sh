#!/usr/bin/env bash
# On-demand launcher for the Foundry Workshop — NO systemd/launchd needed.
#
# Sources your env file, keeps the Mac awake while it runs, and starts the server
# in the FOREGROUND (Ctrl-C to stop). To keep it alive after you close the terminal,
# run it inside tmux/screen or via nohup (see deploy/README.md → "Run on demand").
#
# On-demand is fine for long runs: the server resumes any in-flight build from its
# durable checkpoint on the NEXT start, so a manual restart picks up where it left
# off. (A service adds only AUTO-restart on crash/reboot.)
#
#   ./deploy/run.sh                 # foreground; Ctrl-C to stop
#   FOUNDRY_ENV_FILE=/path/env ./deploy/run.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${FOUNDRY_ENV_FILE:-$REPO_ROOT/deploy/foundry.env}"

if [ -f "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
  echo "loaded env: $ENV_FILE" >&2
else
  echo "note: no env file at $ENV_FILE — using process env only" >&2
fi

cd "$REPO_ROOT"
CMD=(uv run python -m webapp.server)

# macOS: prevent sleep for the lifetime of the run (a sleeping Mac stalls a build).
if [ "$(uname -s)" = "Darwin" ] && command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -is "${CMD[@]}"
fi
exec "${CMD[@]}"
