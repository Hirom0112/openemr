#!/usr/bin/env bash
# =============================================================================
# 01-register-fhir-client.sh
#
# Registers the copilot-agent-v1 OAuth2 client in OpenEMR via the SMART
# dynamic client registration endpoint.  Prints the client_id and
# client_secret that must be set as Railway env vars on the agent-api service.
#
# Usage:
#   ./scripts/01-register-fhir-client.sh
#
# Prerequisites:
#   - curl installed
#   - OpenEMR instance reachable at OPENEMR_BASE_URL
#
# Output:
#   Prints CLIENT_ID and CLIENT_SECRET to stdout.
#   Copy these into scripts/02-deploy-railway.sh or Railway dashboard.
# =============================================================================

set -euo pipefail

OPENEMR_BASE_URL="${OPENEMR_BASE_URL:-https://your-openemr-service.up.railway.app}"
REGISTRATION_ENDPOINT="${OPENEMR_BASE_URL}/oauth2/default/registration"

echo "==> Registering copilot-agent-v1 OAuth2 client at ${REGISTRATION_ENDPOINT}"

RESPONSE=$(curl -s -X POST "${REGISTRATION_ENDPOINT}" \
  -H "Content-Type: application/json" \
  -d '{
    "application_type": "private",
    "redirect_uris": ["https://oauth.pstmn.io/v1/callback"],
    "client_name": "copilot-agent-v1",
    "token_endpoint_auth_method": "client_secret_post",
    "contacts": ["admin@clinical-copilot.internal"],
    "scope": "system/Patient.rs system/Encounter.rs system/Observation.rs system/Condition.rs system/MedicationRequest.rs system/AllergyIntolerance.rs system/DiagnosticReport.rs openid",
    "grant_types": ["client_credentials"],
    "response_types": ["token"]
  }')

echo ""
echo "==> Raw response:"
echo "${RESPONSE}" | python3 -m json.tool 2>/dev/null || echo "${RESPONSE}"

CLIENT_ID=$(echo "${RESPONSE}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('client_id','ERROR'))" 2>/dev/null || echo "")
CLIENT_SECRET=$(echo "${RESPONSE}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('client_secret','ERROR'))" 2>/dev/null || echo "")

if [[ -z "${CLIENT_ID}" || "${CLIENT_ID}" == "ERROR" ]]; then
  echo ""
  echo "ERROR: Registration failed. The response above shows why."
  echo ""
  echo "Common causes:"
  echo "  - OpenEMR dynamic registration may require an initial access token."
  echo "    If so, log into OpenEMR admin → Admin → Config → Certificates/OAuth"
  echo "    and manually create a client_credentials client, then run script 02."
  echo "  - Check that the OPENEMR_BASE_URL is correct: ${OPENEMR_BASE_URL}"
  exit 1
fi

echo ""
echo "============================================================"
echo "SUCCESS — copy these into Railway env vars for agent-api:"
echo "============================================================"
echo "FHIR_CLIENT_ID=${CLIENT_ID}"
echo "FHIR_CLIENT_SECRET=${CLIENT_SECRET}"
echo "============================================================"
echo ""
echo "Next: run scripts/02-deploy-railway.sh with these values."
