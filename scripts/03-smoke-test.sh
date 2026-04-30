#!/usr/bin/env bash
# =============================================================================
# 03-smoke-test.sh
#
# End-to-end smoke test for the deployed Clinical Co-Pilot stack.
# Tests each layer: agent-api health, FHIR auth, FHIR patient data, dispatcher.
#
# Usage:
#   export AGENT_API_URL=https://copilot-agent-api-production.up.railway.app
#   ./scripts/03-smoke-test.sh
#
# All tests print PASS or FAIL with details. Exits 0 if all pass, 1 if any fail.
# =============================================================================

set -euo pipefail

AGENT_API_URL="${AGENT_API_URL:-}"
OPENEMR_BASE_URL="${OPENEMR_BASE_URL:-https://your-openemr-service.up.railway.app}"

PASS=0
FAIL=0

pass() { echo "  PASS: $1"; ((PASS++)) || true; }
fail() { echo "  FAIL: $1"; ((FAIL++)) || true; }

if [[ -z "${AGENT_API_URL}" ]]; then
  echo "ERROR: AGENT_API_URL is not set."
  echo "Export it: export AGENT_API_URL=https://<your-railway-agent-api-url>"
  exit 1
fi

echo ""
echo "============================================================"
echo "Clinical Co-Pilot Smoke Test"
echo "  agent-api: ${AGENT_API_URL}"
echo "  openemr:   ${OPENEMR_BASE_URL}"
echo "============================================================"

# ── Test 1: agent-api health ─────────────────────────────────────────────────
echo ""
echo "[1] agent-api /health"
HEALTH=$(curl -sf "${AGENT_API_URL}/health" 2>/dev/null || echo "")
if echo "${HEALTH}" | grep -q "ok\|healthy"; then
  pass "agent-api is reachable and healthy: ${HEALTH}"
else
  fail "agent-api /health failed. Response: '${HEALTH}'"
  echo "     Check Railway logs: railway logs --service copilot-agent-api"
fi

# ── Test 2: OpenEMR FHIR endpoint reachable ──────────────────────────────────
echo ""
echo "[2] OpenEMR FHIR metadata"
# OpenEMR on Railway returns HTTP 200 with empty body for /fhir/metadata (no CapabilityStatement).
# Check HTTP status code only — body content is not reliable on this deployment.
FHIR_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "${OPENEMR_BASE_URL}/apis/default/fhir/metadata" 2>/dev/null || echo "000")
if [[ "${FHIR_HTTP}" == "200" ]]; then
  pass "FHIR endpoint reachable (HTTP 200)"
else
  fail "FHIR metadata endpoint unreachable: HTTP ${FHIR_HTTP}"
fi

# ── Test 3: FHIR auth token fetch (via agent-api proxy) ─────────────────────
echo ""
echo "[3] FHIR patient proxy (confirms agent-api can authenticate to FHIR)"
# Use pt-001 (Marcus Webb — always present in synthetic dataset)
PATIENT=$(curl -sf "${AGENT_API_URL}/fhir/patient/pt-001" 2>/dev/null || echo "")
if echo "${PATIENT}" | grep -q "resourceType"; then
  pass "FHIR auth working — patient pt-001 returned"
elif echo "${PATIENT}" | grep -qi "unauthorized\|401\|invalid_client"; then
  fail "FHIR auth failed — FHIR_CLIENT_ID/SECRET may be wrong or client not registered"
  echo "     Run scripts/01-register-fhir-client.sh to register the OAuth client"
else
  fail "Unexpected response: '${PATIENT:0:300}'"
fi

# ── Test 4: dispatcher POST /agent/query ─────────────────────────────────────
echo ""
echo "[4] POST /agent/query — census summary (UC-1 triage)"
DISPATCH=$(curl -sf -X POST "${AGENT_API_URL}/agent/query" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Good morning. Please give me the morning triage list.",
    "session_id": "smoke-test-session-001",
    "provider_id": "smoke-test-provider",
    "patient_ids": ["pt-001", "pt-002", "pt-003"]
  }' 2>/dev/null || echo "")

if echo "${DISPATCH}" | grep -q '"type"'; then
  DTYPE=$(echo "${DISPATCH}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('type','?'))" 2>/dev/null || echo "?")
  pass "Dispatcher responded with type: ${DTYPE}"
elif echo "${DISPATCH}" | grep -qi "error"; then
  fail "Dispatcher returned error: '${DISPATCH:0:400}'"
else
  fail "Dispatcher returned unexpected response: '${DISPATCH:0:400}'"
fi

# ── Test 5: triage rationale direct endpoint ─────────────────────────────────
echo ""
echo "[5] POST /agent/triage_rationale — click-to-expand (UC-1 rationale)"
RATIONALE=$(curl -sf -X POST "${AGENT_API_URL}/agent/triage_rationale/pt-001" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "smoke-test-session-001"}' 2>/dev/null || echo "")

if echo "${RATIONALE}" | grep -q '"triage_level"\|"level"\|"label"\|"rationale"'; then
  pass "Triage rationale endpoint responded with scoring data"
elif echo "${RATIONALE}" | grep -qi "error"; then
  fail "Triage rationale returned error: '${RATIONALE:0:400}'"
else
  fail "Triage rationale unexpected response: '${RATIONALE:0:400}'"
fi

# ── Test 6: Marcus Webb (pt-001) is ranked P1 (qSOFA) ───────────────────────
echo ""
echo "[6] Verify Marcus Webb (pt-001) is ranked P1 (qSOFA ≥2)"
if echo "${DISPATCH}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
patients = d.get('data', {}).get('patients', [])
if not patients:
    patients = d.get('data', [])
for p in patients:
    if p.get('patient_id') == 'pt-001' or 'Webb' in str(p):
        level = p.get('priority_level', p.get('level', 0))
        print(f'pt-001 priority level: {level}')
        sys.exit(0 if str(level) == '1' else 1)
print('pt-001 not found in response')
sys.exit(1)
" 2>/dev/null; then
  pass "Marcus Webb correctly ranked P1"
else
  fail "Marcus Webb not at P1 or not found in census response — check FHIR data"
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "============================================================"

if [[ ${FAIL} -gt 0 ]]; then
  echo ""
  echo "Some tests failed. Fix the failures above before running 04-verify-gates.sh"
  exit 1
else
  echo ""
  echo "All smoke tests passed. Run 04-verify-gates.sh to check cutover gates."
fi
