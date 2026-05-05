#!/usr/bin/env bash
# Bring up (or rebuild) the local OpenEMR + agent-api stack with the
# copilot overlay and .env.copilot loaded. All extra args are forwarded
# to `docker compose up`, so:
#
#   ./up.sh                    # start / refresh
#   ./up.sh --build agent-api  # rebuild just the agent-api image
#   ./up.sh --build            # rebuild everything
set -euo pipefail
cd "$(dirname "$0")"
exec docker compose \
  --env-file .env.copilot \
  -f docker-compose.yml \
  -f docker-compose.copilot.yml \
  up -d "$@"
