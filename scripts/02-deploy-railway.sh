#!/usr/bin/env bash
# =============================================================================
# 02-deploy-railway.sh
#
# Deploys the Clinical Co-Pilot agent-api service to Railway.
#
# What this script does:
#   1. Verifies Railway CLI is authenticated and linked to the right project
#   2. Creates the copilot-agent-api service (if it doesn't exist)
#   3. Creates the copilot-redis service (if it doesn't exist)
#   4. Sets all required environment variables on agent-api
#   5. Sets COPILOT_AGENT_API_URL on the openemr service
#   6. Triggers a deploy of agent-api from the agent-api/ subdirectory
#
# Usage:
#   export FHIR_CLIENT_ID=...
#   export FHIR_CLIENT_SECRET=...
#   export ANTHROPIC_API_KEY=...
#   export LANGFUSE_PUBLIC_KEY=...
#   export LANGFUSE_SECRET_KEY=...
#   ./scripts/02-deploy-railway.sh
#
# Prerequisites:
#   - railway CLI installed and authenticated (railway whoami should succeed)
#   - All 5 env vars above exported before running
#   - Run from the repo root (openemr/)
# =============================================================================

set -euo pipefail

OPENEMR_SERVICE="clinical-copilot-openemr"
AGENT_SERVICE="copilot-agent-api"
REDIS_SERVICE="copilot-redis"
OPENEMR_BASE_URL="https://your-openemr-service.up.railway.app"

# ── Validate required vars ───────────────────────────────────────────────────
for var in FHIR_CLIENT_ID FHIR_CLIENT_SECRET ANTHROPIC_API_KEY LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY; do
  if [[ -z "${!var:-}" ]]; then
    echo "ERROR: ${var} is not set. Export it before running this script."
    exit 1
  fi
done

# ── Validate Railway CLI ─────────────────────────────────────────────────────
if ! command -v railway &>/dev/null; then
  echo "ERROR: railway CLI not found. Install from https://docs.railway.app/develop/cli"
  exit 1
fi

echo "==> Railway project context:"
railway status

# ── Step 1: Create Redis service ─────────────────────────────────────────────
echo ""
echo "==> Checking Redis service..."
if railway service "${REDIS_SERVICE}" 2>/dev/null | grep -q "Online\|Deploying"; then
  echo "    Redis service already exists — skipping creation."
else
  echo "    Creating Redis service (Railway managed Redis)..."
  # Railway doesn't support `service create` for managed databases via CLI yet.
  # Print instructions and pause for the user to do this one step in the dashboard.
  echo ""
  echo "  ┌─────────────────────────────────────────────────────────────────┐"
  echo "  │  MANUAL STEP REQUIRED (30 seconds):                            │"
  echo "  │  1. Open: https://railway.app/project/4322141a-4916-4d6a-a1ab-204340735b75  │"
  echo "  │  2. Click '+ New' → 'Database' → 'Add Redis'                  │"
  echo "  │  3. Name it: copilot-redis                                     │"
  echo "  │  4. Copy the REDIS_URL from its Variables tab                  │"
  echo "  │  5. Export it here: export REDIS_URL=<paste>                   │"
  echo "  └─────────────────────────────────────────────────────────────────┘"
  echo ""
  read -r -p "Press ENTER once Redis is created and REDIS_URL is exported, or Ctrl+C to abort: "
fi

if [[ -z "${REDIS_URL:-}" ]]; then
  echo "ERROR: REDIS_URL is not set. Export the Redis internal URL from Railway."
  exit 1
fi

# ── Step 2: Deploy agent-api ──────────────────────────────────────────────────
echo ""
echo "==> Deploying agent-api from agent-api/ subdirectory..."

# Set all env vars on the agent-api service, then deploy
railway service "${AGENT_SERVICE}" 2>/dev/null || true

pushd agent-api > /dev/null

railway variables set \
  OPENEMR_BASE_URL="${OPENEMR_BASE_URL}" \
  FHIR_CLIENT_ID="${FHIR_CLIENT_ID}" \
  FHIR_CLIENT_SECRET="${FHIR_CLIENT_SECRET}" \
  FHIR_TOKEN_URL="${OPENEMR_BASE_URL}/oauth2/default/token" \
  ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY}" \
  REDIS_URL="${REDIS_URL}" \
  LANGFUSE_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY}" \
  LANGFUSE_SECRET_KEY="${LANGFUSE_SECRET_KEY}" \
  LANGFUSE_HOST="https://cloud.langfuse.com" \
  LOG_LEVEL="INFO" \
  LEGACY_ENDPOINTS_ENABLED="true" \
  2>&1 || echo "  Note: variable set may require service to exist first — continuing..."

echo "==> Pushing agent-api to Railway..."
railway up --detach

popd > /dev/null

# ── Step 3: Get the agent-api public URL and set it on OpenEMR ───────────────
echo ""
echo "==> Waiting for agent-api to deploy (up to 90 seconds)..."
sleep 15

AGENT_API_URL=$(railway service "${AGENT_SERVICE}" 2>/dev/null | grep -o 'https://[^ ]*' | head -1 || echo "")

if [[ -z "${AGENT_API_URL}" ]]; then
  echo ""
  echo "  Could not auto-detect agent-api URL yet (deploy still in progress)."
  echo "  After deploy completes, find the URL in Railway dashboard and run:"
  echo ""
  echo "    railway variables set COPILOT_AGENT_API_URL=<url> --service ${OPENEMR_SERVICE}"
  echo ""
else
  echo "==> agent-api URL: ${AGENT_API_URL}"
  echo "==> Setting COPILOT_AGENT_API_URL on OpenEMR service..."
  railway variables set "COPILOT_AGENT_API_URL=${AGENT_API_URL}" --service "${OPENEMR_SERVICE}" 2>/dev/null || \
    echo "  Note: set this manually in Railway dashboard if the above failed."
fi

echo ""
echo "============================================================"
echo "Deploy triggered. Next steps:"
echo "  1. Watch logs: railway logs --service ${AGENT_SERVICE}"
echo "  2. Once healthy, run: ./scripts/03-smoke-test.sh"
echo "============================================================"
