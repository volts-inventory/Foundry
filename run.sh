#!/usr/bin/env bash
# On-demand launcher for the Foundry Workshop.
#
# Sources your env file (foundry.env), keeps the Mac awake while it runs, and starts
# the server in the FOREGROUND (Ctrl-C to stop). To keep it alive after you close the
# terminal, run it inside tmux/screen or via nohup:
#   tmux new -s foundry './run.sh'          # reattach later: tmux attach -t foundry
#   nohup ./run.sh > ~/foundry.log 2>&1 &   # detached; kill the PID to stop
#
# Long runs are fine: the server resumes any in-flight build from its durable
# checkpoint on the NEXT start, so a manual restart picks up where it left off.
#
#   ./run.sh                        # foreground; Ctrl-C to stop
#   FOUNDRY_ENV_FILE=/path/env ./run.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${FOUNDRY_ENV_FILE:-$REPO_ROOT/foundry.env}"

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
