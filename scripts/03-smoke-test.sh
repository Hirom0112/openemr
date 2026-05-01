#!/usr/bin/env bash
# =============================================================================
# 03-smoke-test.sh
#
# End-to-end smoke test for the deployed Clinical Co-Pilot stack.
# Covers: health, FHIR auth, and one assertion per UC-1..UC-5 + rationale path.
#
# Usage:
#   export AGENT_API_URL=https://copilot-agent-api-production.up.railway.app
#   export OPENEMR_BASE_URL=https://your-openemr-service.up.railway.app   # optional
#   ./scripts/03-smoke-test.sh
#
# Required test data: pt-001 (Marcus Webb) must exist in the synthetic dataset.
# Env vars: see docs/clinical-copilot/smoke-matrix.md §Required env vars.
#
# All checks print PASS or FAIL with a one-line reason.
# Exits 0 if all checks pass, 1 if any fail.
# =============================================================================

set -euo pipefail

AGENT_API_URL="${AGENT_API_URL:-}"
OPENEMR_BASE_URL="${OPENEMR_BASE_URL:-https://your-openemr-service.up.railway.app}"
SMOKE_SESSION="smoke-test-$(date +%s)"
SMOKE_PROVIDER="smoke-provider"
SMOKE_PATIENTS='["pt-001","pt-002","pt-003"]'
SMOKE_PATIENT_PRIMARY="pt-001"

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
echo "  agent-api:  ${AGENT_API_URL}"
echo "  openemr:    ${OPENEMR_BASE_URL}"
echo "  session:    ${SMOKE_SESSION}"
echo "============================================================"

# ── Helper: send a dispatcher query and capture response ─────────────────────

dispatcher_query() {
  local message="$1"
  curl -sf -X POST "${AGENT_API_URL}/agent/query" \
    -H "Content-Type: application/json" \
    -d "{
      \"message\": ${message},
      \"session_id\": \"${SMOKE_SESSION}\",
      \"provider_id\": \"${SMOKE_PROVIDER}\",
      \"patient_ids\": ${SMOKE_PATIENTS}
    }" 2>/dev/null || echo ""
}

# ── Helper: extract response type field ──────────────────────────────────────

response_type() {
  python3 -c "import sys,json; print(json.load(sys.stdin).get('type',''))" 2>/dev/null || echo ""
}

# ── Check 1: agent-api /health ───────────────────────────────────────────────
echo ""
echo "[1] agent-api /health"
HEALTH=$(curl -sf "${AGENT_API_URL}/health" 2>/dev/null || echo "")
if echo "${HEALTH}" | grep -q '"status"'; then
  STATUS=$(echo "${HEALTH}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
  if [[ "${STATUS}" == "ok" ]]; then
    pass "agent-api healthy (status=ok)"
  else
    fail "agent-api /health returned status '${STATUS}' — expected 'ok'"
  fi
else
  fail "agent-api /health unreachable. Response: '${HEALTH:0:200}'"
  echo "     Check Railway logs: railway logs --service copilot-agent-api"
fi

# ── Check 2: OpenEMR FHIR endpoint reachable ─────────────────────────────────
echo ""
echo "[2] OpenEMR FHIR metadata"
FHIR_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 \
  "${OPENEMR_BASE_URL}/apis/default/fhir/metadata" 2>/dev/null || echo "000")
if [[ "${FHIR_HTTP}" == "200" ]]; then
  pass "FHIR endpoint reachable (HTTP 200)"
else
  fail "FHIR metadata endpoint unreachable: HTTP ${FHIR_HTTP}"
fi

# ── Check 3: FHIR auth token (via agent-api proxy) ───────────────────────────
echo ""
echo "[3] FHIR patient proxy — confirms agent-api can authenticate to FHIR"
PATIENT=$(curl -sf "${AGENT_API_URL}/fhir/patient/${SMOKE_PATIENT_PRIMARY}" 2>/dev/null || echo "")
if echo "${PATIENT}" | grep -q '"resourceType"'; then
  pass "FHIR auth working — patient ${SMOKE_PATIENT_PRIMARY} returned"
elif echo "${PATIENT}" | grep -qi "unauthorized\|401\|invalid_client"; then
  fail "FHIR auth failed — FHIR_CLIENT_ID/SECRET may be wrong or client not registered"
  echo "     Run scripts/01-register-fhir-client.sh to register the OAuth client"
else
  fail "Unexpected FHIR proxy response: '${PATIENT:0:300}'"
fi

# ── Check 4: UC-1 — Morning priority triage (census) ─────────────────────────
echo ""
echo "[4] UC-1: POST /agent/query with __census_summary__ — expects type=census"
UC1_RESP=$(dispatcher_query '"__census_summary__"')
UC1_TYPE=$(echo "${UC1_RESP}" | response_type)

if [[ "${UC1_TYPE}" == "census" ]]; then
  # Verify ranked entries exist
  ENTRY_COUNT=$(echo "${UC1_RESP}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
patients = d.get('data', {}).get('census', [])
if not patients:
    patients = d.get('data', {}).get('patients', d.get('data', []))
print(len(patients) if isinstance(patients, list) else 0)
" 2>/dev/null || echo "0")
  if [[ "${ENTRY_COUNT}" -gt 0 ]]; then
    pass "UC-1 census: type=census, ${ENTRY_COUNT} ranked entries returned"
  else
    fail "UC-1 census: type=census but no ranked entries in response — check get_census_summary tool"
  fi
elif [[ -z "${UC1_RESP}" ]]; then
  fail "UC-1 census: dispatcher returned empty response"
else
  fail "UC-1 census: expected type=census, got type='${UC1_TYPE}'. Response: '${UC1_RESP:0:400}'"
fi

# ── Check 5: UC-2 — Pre-encounter patient briefing ───────────────────────────
echo ""
echo "[5] UC-2: POST /agent/query with patient briefing prompt — expects type=briefing"
UC2_RESP=$(dispatcher_query '"Brief me on patient pt-001."')
UC2_TYPE=$(echo "${UC2_RESP}" | response_type)

if [[ "${UC2_TYPE}" == "briefing" ]]; then
  NARRATIVE=$(echo "${UC2_RESP}" | python3 -c "
import sys, json
print(json.load(sys.stdin).get('narrative',''))
" 2>/dev/null || echo "")
  if [[ -n "${NARRATIVE}" ]]; then
    pass "UC-2 briefing: type=briefing, narrative present"
  else
    fail "UC-2 briefing: type=briefing but narrative field is empty — check get_patient_briefing tool"
  fi
elif [[ -z "${UC2_RESP}" ]]; then
  fail "UC-2 briefing: dispatcher returned empty response"
else
  fail "UC-2 briefing: expected type=briefing, got type='${UC2_TYPE}'. Response: '${UC2_RESP:0:400}'"
fi

# ── Check 6: UC-3 — Targeted record query ────────────────────────────────────
echo ""
echo "[6] UC-3: POST /agent/query with targeted record question — expects type=query_answer"
UC3_RESP=$(dispatcher_query '"What was the most recent potassium level for patient pt-001?"')
UC3_TYPE=$(echo "${UC3_RESP}" | response_type)

if [[ "${UC3_TYPE}" == "query_answer" ]]; then
  pass "UC-3 query: type=query_answer returned"
elif [[ "${UC3_TYPE}" == "text" ]]; then
  # Acceptable fallback: dispatcher may return text for not-found queries — verify narrative
  NARRATIVE=$(echo "${UC3_RESP}" | python3 -c "
import sys, json
print(json.load(sys.stdin).get('narrative',''))
" 2>/dev/null || echo "")
  if [[ -n "${NARRATIVE}" ]]; then
    pass "UC-3 query: type=text (not-found fallback), narrative present — acceptable for missing data"
  else
    fail "UC-3 query: type=text but narrative empty — check query_patient_records tool"
  fi
elif [[ -z "${UC3_RESP}" ]]; then
  fail "UC-3 query: dispatcher returned empty response"
else
  fail "UC-3 query: expected type=query_answer or text, got type='${UC3_TYPE}'. Response: '${UC3_RESP:0:400}'"
fi

# ── Check 7: UC-4 — Medication safety surface ────────────────────────────────
echo ""
echo "[7] UC-4: POST /agent/query with medication safety prompt — expects type=medication_safety"
UC4_RESP=$(dispatcher_query '"Are there any medication safety concerns I should know about for patient pt-001?"')
UC4_TYPE=$(echo "${UC4_RESP}" | response_type)

if [[ "${UC4_TYPE}" == "medication_safety" ]]; then
  pass "UC-4 medication safety: type=medication_safety returned"
elif [[ -z "${UC4_RESP}" ]]; then
  fail "UC-4 medication safety: dispatcher returned empty response"
else
  fail "UC-4 medication safety: expected type=medication_safety, got type='${UC4_TYPE}'. Response: '${UC4_RESP:0:400}'"
fi

# ── Check 8: UC-5 — End-of-rounds handoff generation ─────────────────────────
echo ""
echo "[8] UC-5: POST /agent/query with handoff prompt — expects type=handoff"
UC5_RESP=$(dispatcher_query '"Generate handoff notes for all my patients."')
UC5_TYPE=$(echo "${UC5_RESP}" | response_type)

if [[ "${UC5_TYPE}" == "handoff" ]]; then
  pass "UC-5 handoff: type=handoff returned"
elif [[ -z "${UC5_RESP}" ]]; then
  fail "UC-5 handoff: dispatcher returned empty response"
else
  fail "UC-5 handoff: expected type=handoff, got type='${UC5_TYPE}'. Response: '${UC5_RESP:0:400}'"
fi

# ── Check 9: Direct triage rationale (click-to-expand) ───────────────────────
echo ""
echo "[9] Direct rationale: POST /agent/triage_rationale/${SMOKE_PATIENT_PRIMARY} — expects rationale fields"
RATIONALE=$(curl -sf -X POST "${AGENT_API_URL}/agent/triage_rationale/${SMOKE_PATIENT_PRIMARY}" \
  -H "Content-Type: application/json" \
  -d "{\"patient_id\": \"${SMOKE_PATIENT_PRIMARY}\", \"session_id\": \"${SMOKE_SESSION}\"}" \
  2>/dev/null || echo "")

if echo "${RATIONALE}" | grep -q '"triage_level"\|"level"\|"label"\|"rationale"\|"criteria"'; then
  pass "Triage rationale: scoring/rationale data returned for ${SMOKE_PATIENT_PRIMARY}"
elif echo "${RATIONALE}" | grep -qi "error"; then
  fail "Triage rationale: error response — '${RATIONALE:0:400}'"
else
  fail "Triage rationale: unexpected response (no rationale fields) — '${RATIONALE:0:300}'"
fi

# ── Check 10: UC-1 priority ordering (pt-001 ranked P1) ──────────────────────
echo ""
echo "[10] UC-1 ordering: verify pt-001 (Marcus Webb) ranked at priority level 1"
if echo "${UC1_RESP}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
patients = d.get('data', {}).get('census', [])
if not patients:
    patients = d.get('data', {}).get('patients', d.get('data', []))
if not isinstance(patients, list):
    print('no census list'); sys.exit(1)
for p in patients:
    pid = p.get('patient_id', p.get('id', ''))
    name = str(p.get('name', ''))
    if pid == 'pt-001' or 'Webb' in name:
        level = p.get('triage_level', p.get('priority_level', p.get('level', None)))
        print(f'pt-001 triage_level={level}')
        sys.exit(0 if str(level) == '1' else 1)
print('pt-001 not found in census list')
sys.exit(1)
" 2>/dev/null; then
  pass "UC-1 ordering: pt-001 (Marcus Webb) correctly ranked at priority level 1"
else
  fail "UC-1 ordering: pt-001 not ranked P1 or not found — check FHIR synthetic data for pt-001"
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "============================================================"

if [[ ${FAIL} -gt 0 ]]; then
  echo ""
  echo "Some checks failed. Fix failures above before running 04-verify-gates.sh"
  echo "See docs/clinical-copilot/smoke-matrix.md for test data requirements."
  exit 1
else
  echo ""
  echo "All smoke checks passed. Run 04-verify-cutover-gates.sh to verify cutover gates."
fi
