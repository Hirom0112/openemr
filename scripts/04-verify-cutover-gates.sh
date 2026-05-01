#!/usr/bin/env bash
# =============================================================================
# 04-verify-cutover-gates.sh
#
# Verifies all Phase 13 cutover gates against the live Railway deployment.
# Gates from AGENT_CONTRACT.md §2 and §6:
#   1. Routing accuracy >= 95% on 20-query eval set
#   2. Dispatcher p95 latency <= 4s
#   3. Click-to-expand rationale < 2s
#   4. Error rate <= 1%          (MANUAL — requires Grafana/Railway metrics)
#   5. Tool misroute rate <= 2%  (MANUAL — requires Langfuse traces)
#   6. Cache-hit tokens >= 70%   (MANUAL — requires Langfuse token telemetry)
#
# Usage:
#   export AGENT_API_URL=https://copilot-agent-api-production.up.railway.app
#   ./scripts/04-verify-cutover-gates.sh
#
# Routing accuracy uses response type field (census/briefing/query_answer/
# medication_safety/handoff) when metadata.tool_called is absent.
#
# Exits 0 if automated gates pass, 1 if any automated gate fails.
# =============================================================================

set -euo pipefail

AGENT_API_URL="${AGENT_API_URL:-}"
SESSION_ID="gate-check-$(date +%s)"
GATE_PROVIDER="gate-check"
GATE_PATIENTS='["pt-001","pt-002","pt-003"]'

PASS=0
FAIL=0
WARN=0

pass()   { echo "  PASS: $1"; ((PASS++))  || true; }
fail()   { echo "  FAIL: $1"; ((FAIL++))  || true; }
warn()   { echo "  WARN: $1"; ((WARN++))  || true; }
ms_now() { python3 -c "import time; print(int(time.time() * 1000))"; }

# Run a dispatcher call, capture elapsed ms, write response to /tmp/gate_resp.json
timed_query() {
  local message="$1"
  local start end
  start=$(ms_now)
  curl -sf -X POST "${AGENT_API_URL}/agent/query" \
    -H "Content-Type: application/json" \
    -d "{
      \"message\": ${message},
      \"session_id\": \"${SESSION_ID}\",
      \"provider_id\": \"${GATE_PROVIDER}\",
      \"patient_ids\": ${GATE_PATIENTS}
    }" > /tmp/gate_resp.json 2>/dev/null || echo "{}" > /tmp/gate_resp.json
  end=$(ms_now)
  echo $(( end - start ))
}

# Resolve which tool was invoked from a dispatcher response JSON string.
# Prefers metadata.tool_called; falls back to type→tool mapping.
resolve_tool() {
  python3 -c "
import sys, json
TYPE_TO_TOOL = {
    'census':           'get_census_summary',
    'briefing':         'get_patient_briefing',
    'query_answer':     'query_patient_records',
    'medication_safety':'get_medication_safety',
    'handoff':          'generate_handoff',
}
d = json.load(sys.stdin)
meta = d.get('metadata', {})
tool = meta.get('tool_called', '')
if not tool:
    tool = TYPE_TO_TOOL.get(d.get('type', ''), d.get('type', 'unknown'))
print(tool)
" 2>/dev/null || echo "unknown"
}

if [[ -z "${AGENT_API_URL}" ]]; then
  echo "ERROR: AGENT_API_URL is not set."
  exit 1
fi

echo ""
echo "============================================================"
echo "Phase 13 Cutover Gate Verification"
echo "  agent-api: ${AGENT_API_URL}"
echo "  session:   ${SESSION_ID}"
echo "============================================================"

# ── Gate 1: Routing accuracy — 20 representative queries ─────────────────────
echo ""
echo "[Gate 1] Routing accuracy on 20-query eval set (target >= 95%)"
echo "  Dispatching queries with required session_id, provider_id, patient_ids..."

# Each entry: "expected_tool|message"
# Messages are deterministic strings that should reliably route to the expected tool.
QUERIES=(
  "get_census_summary|__census_summary__"
  "get_census_summary|Give me the morning triage list."
  "get_patient_briefing|Brief me on Marcus Webb in bed 501."
  "get_patient_briefing|What is going on with patient pt-001?"
  "get_patient_briefing|Pre-encounter briefing for Delia Fontaine."
  "get_patient_briefing|What happened overnight with patient pt-002?"
  "query_patient_records|What was the last potassium on patient pt-001?"
  "query_patient_records|When was the last chest X-ray for Fontaine?"
  "query_patient_records|What is the creatinine trend for patient pt-001?"
  "query_patient_records|Has patient pt-001 been on steroids before?"
  "get_medication_safety|Are there any allergy concerns with patient pt-001 medications?"
  "get_medication_safety|Check medication safety for patient pt-001."
  "get_medication_safety|What chart data is relevant to metoprolol for pt-002?"
  "generate_handoff|Generate handoff notes for all my patients."
  "generate_handoff|Give me handoff for the full census."
  "get_patient_briefing|Tell me about the patient in bed 502."
  "query_patient_records|What did the last echo show for patient pt-001?"
  "get_medication_safety|Medication safety surface for patient pt-002."
  "generate_handoff|Prepare end-of-rounds handoff for all patients."
  "get_census_summary|Show me the priority triage list for this morning."
)

CORRECT=0
TOTAL=${#QUERIES[@]}

for entry in "${QUERIES[@]}"; do
  EXPECTED="${entry%%|*}"
  MSG="${entry#*|}"

  RESP=$(curl -sf -X POST "${AGENT_API_URL}/agent/query" \
    -H "Content-Type: application/json" \
    -d "{
      \"message\": \"${MSG}\",
      \"session_id\": \"${SESSION_ID}\",
      \"provider_id\": \"${GATE_PROVIDER}\",
      \"patient_ids\": ${GATE_PATIENTS}
    }" 2>/dev/null || echo "{}")

  TOOL_CALLED=$(echo "${RESP}" | resolve_tool)

  if [[ "${TOOL_CALLED}" == "${EXPECTED}" ]]; then
    ((CORRECT++)) || true
    echo "    MATCH  [${EXPECTED}] ← \"${MSG:0:55}\""
  else
    echo "    MISS   expected=${EXPECTED} got=${TOOL_CALLED} ← \"${MSG:0:55}\""
  fi
done

ACCURACY=$(( CORRECT * 100 / TOTAL ))
if [[ ${ACCURACY} -ge 95 ]]; then
  pass "Routing accuracy: ${CORRECT}/${TOTAL} = ${ACCURACY}% (target >= 95%)"
else
  fail "Routing accuracy: ${CORRECT}/${TOTAL} = ${ACCURACY}% (target >= 95%)"
fi

# ── Gate 2: Dispatcher p95 latency <= 4s ─────────────────────────────────────
echo ""
echo "[Gate 2] Dispatcher p95 latency (target <= 4000ms) — 10 timed queries"

LATENCIES=()
for i in $(seq 1 10); do
  MS=$(timed_query '"Brief me on patient pt-001."')
  LATENCIES+=("${MS}")
  echo "    query ${i}: ${MS}ms"
done

# p95 = max of 10 samples (conservative but correct for n=10)
P95=$(printf '%s\n' "${LATENCIES[@]}" | sort -n | tail -1)
if [[ ${P95} -le 4000 ]]; then
  pass "p95 latency: ${P95}ms (target <= 4000ms)"
elif [[ ${P95} -le 8000 ]]; then
  fail "p95 latency: ${P95}ms — above 4s target. Investigate Redis cache-hit rate and FHIR cold path."
else
  fail "p95 latency: ${P95}ms — significantly above 4s target. Check FHIR cold-path calls in logs."
fi

# ── Gate 3: Click-to-expand rationale < 2s ───────────────────────────────────
echo ""
echo "[Gate 3] Click-to-expand triage rationale (target <= 2000ms)"

RAT_START=$(ms_now)
curl -sf -X POST "${AGENT_API_URL}/agent/triage_rationale/pt-001" \
  -H "Content-Type: application/json" \
  -d "{\"patient_id\": \"pt-001\", \"session_id\": \"${SESSION_ID}\"}" \
  > /tmp/gate_rationale.json 2>/dev/null || echo "{}" > /tmp/gate_rationale.json
RAT_END=$(ms_now)
RAT_MS=$(( RAT_END - RAT_START ))

if [[ ${RAT_MS} -le 2000 ]]; then
  pass "Triage rationale: ${RAT_MS}ms (target <= 2000ms)"
else
  fail "Triage rationale: ${RAT_MS}ms — above 2s target. Redis or rules-engine latency issue."
fi

# ── Gate 4: Error rate <= 1% ─────────────────────────────────────────────────
echo ""
echo "[Gate 4] Error rate <= 1% (MANUAL — requires production metrics)"
echo "  Grafana query (Prometheus):"
echo "    rate(http_requests_total{status=~'5..'}[5m]) / rate(http_requests_total[5m])"
echo "  Railway: railway metrics --service copilot-agent-api"
echo "  Acceptable: <= 1% sustained over a 24-hour window during parallel run."
warn "Gate 4 (error rate) — verify manually in Grafana or Railway metrics dashboard"

# ── Gate 5: Tool misroute rate <= 2% ─────────────────────────────────────────
echo ""
echo "[Gate 5] Tool misroute rate <= 2% (MANUAL — requires Langfuse traces)"
echo "  In Langfuse: filter traces by tag 'misroute_detected'."
echo "  Query: misroute_detected events / total agent_query dispatches"
echo "  Acceptable: <= 2% sustained over the Phase 12 parallel-run window."
warn "Gate 5 (misroute rate) — verify manually in Langfuse trace dashboard"

# ── Gate 6: Cache-hit tokens >= 70% for UC-2/3/4 ────────────────────────────
echo ""
echo "[Gate 6] Cache-hit input tokens >= 70% for UC-2/3/4 calls (MANUAL — Langfuse)"
echo "  In Langfuse: filter by tool_name in [get_patient_briefing, query_patient_records,"
echo "  get_medication_safety]. Compare cached_input_tokens / total_input_tokens per session."
echo "  Acceptable: average >= 70% across UC-2/3/4 calls within a session."
echo "  If below 70%: debug cache_control placement in agent/dispatcher.py."
warn "Gate 6 (cache-hit rate) — verify manually in Langfuse token telemetry"

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo "Gate Results: ${PASS} automated passed, ${FAIL} automated failed, ${WARN} manual-verify"
echo "============================================================"

if [[ ${FAIL} -gt 0 ]]; then
  echo ""
  echo "Automated gates failed. Do not flip LEGACY_ENDPOINTS_ENABLED=false until all pass."
  exit 1
else
  echo ""
  if [[ ${WARN} -gt 0 ]]; then
    echo "Automated gates passed. Confirm the ${WARN} manual gates in Grafana/Langfuse."
    echo "Once all manual gates are confirmed, run:"
  else
    echo "All gates passed. Run:"
  fi
  echo "  railway variables set LEGACY_ENDPOINTS_ENABLED=false --service copilot-agent-api"
  echo "  (allow 24h soak with error rate confirmed <= 1% before removing legacy endpoints)"
fi
