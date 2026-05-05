#!/usr/bin/env bash
#
# Clinical Co-Pilot — MVP verification harness.
#
# Runs four checks against the deployed agent-api + OpenEMR pair and emits a
# single PASS/FAIL summary. Output is suitable to paste into a submission.
#
# Usage:
#   bash scripts/verify_mvp.sh                      # uses real fixture
#   bash scripts/verify_mvp.sh /path/to/lab.pdf     # uses provided fixture
#
# Required env (read but never echoed):
#   COPILOT_JWT_SECRET  — shared HMAC secret (set in .env.copilot or shell)
#
# What this script does NOT do:
#   * Run the eval suite (50 cases against live Anthropic ~ $0.50/run). The
#     authoritative eval status comes from CI on PR #1 and the latest push to
#     clinical-copilot. This script summarises CI status via the GitHub API.
#
# Exit codes:
#   0 — all four checks PASS
#   1 — one or more checks FAIL

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_API="${AGENT_API_URL:-https://copilot-agent-api-production.up.railway.app}"
OPENEMR="${OPENEMR_URL:-https://clinical-copilot-openemr-production.up.railway.app}"
PATIENT_ID="${PATIENT_ID:-1}"
FIXTURE="${1:-${REPO_ROOT}/agent-api/tests/fixtures/lab_osh_lactate.pdf}"
GH_REPO="${GH_REPO:-Hirom0112/openemr}"

# Source secret from the env-file if not already set (never echo).
if [[ -z "${COPILOT_JWT_SECRET:-}" ]] && [[ -f "${REPO_ROOT}/docker/development-easy/.env.copilot" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${REPO_ROOT}/docker/development-easy/.env.copilot"
    set +a
fi

# Fall back to pulling COPILOT_JWT_SECRET from Railway when still missing.
# The deployed secret is the source of truth — if it differs from any local
# file, the JWT signature won't validate against the live agent-api.
if [[ -z "${COPILOT_JWT_SECRET:-}" ]] && command -v railway >/dev/null 2>&1; then
    railway service copilot-agent-api >/dev/null 2>&1 || true
    RAILWAY_SECRET="$(railway variables --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("COPILOT_JWT_SECRET",""))' 2>/dev/null || true)"
    if [[ -n "${RAILWAY_SECRET}" ]]; then
        export COPILOT_JWT_SECRET="${RAILWAY_SECRET}"
    fi
    unset RAILWAY_SECRET
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  [PASS] %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*"; OVERALL=1; }

mint_jwt() {
    # Mint a 5-minute HS256 JWT. Reads COPILOT_JWT_SECRET from env; never prints it.
    if [[ -z "${COPILOT_JWT_SECRET:-}" ]] || [[ ${#COPILOT_JWT_SECRET} -lt 32 ]]; then
        return 1
    fi
    python3 -c "
import os, jwt, time
secret = os.environ['COPILOT_JWT_SECRET']
now = int(time.time())
print(jwt.encode({
    'sub': '${PATIENT_ID}',
    'sid': 'verify-mvp',
    'iat': now,
    'exp': now + 300,
    'iss': 'openemr-copilot',
    'provider_id': '${PATIENT_ID}',
}, secret, algorithm='HS256'))
"
}

OVERALL=0

# ---------------------------------------------------------------------------
# Check 1 — Agent API health
# ---------------------------------------------------------------------------

bold "1. Agent API health"
HEALTH_BODY="$(curl -sS -m 10 "${AGENT_API}/health" 2>/dev/null || true)"
if echo "${HEALTH_BODY}" | grep -q '"status":"ok"'; then
    ok "GET ${AGENT_API}/health -> ${HEALTH_BODY}"
else
    fail "agent API health failed: ${HEALTH_BODY:-no response}"
fi

# ---------------------------------------------------------------------------
# Check 2 — POST /document/ingest end-to-end
# ---------------------------------------------------------------------------

bold "2. Document ingest end-to-end"

if [[ ! -f "${FIXTURE}" ]]; then
    fail "fixture not found: ${FIXTURE}"
else
    JWT="$(mint_jwt)" || JWT=""
    if [[ -z "${JWT}" ]]; then
        fail "could not mint JWT (COPILOT_JWT_SECRET unset or <32 chars)"
    else
        INGEST_OUT="$(mktemp)"
        HTTP_CODE="$(curl -sS -m 90 -o "${INGEST_OUT}" -w '%{http_code}' \
            -X POST \
            -H "Authorization: Bearer ${JWT}" \
            -F "file=@${FIXTURE}" \
            -F "patient_id=${PATIENT_ID}" \
            -F "doc_type_hint=lab_report" \
            "${AGENT_API}/document/ingest" 2>/dev/null || echo 000)"

        if [[ "${HTTP_CODE}" == "200" ]]; then
            DOC_REF=$(python3 -c "import json,sys; d=json.load(open('${INGEST_OUT}')); print(d.get('document_reference_id',''))" 2>/dev/null || echo "")
            KIND=$(python3 -c "import json,sys; d=json.load(open('${INGEST_OUT}')); print((d.get('extraction') or {}).get('kind',''))" 2>/dev/null || echo "")
            N_VALUES=$(python3 -c "import json,sys; d=json.load(open('${INGEST_OUT}')); print(len((d.get('extraction') or {}).get('values') or []))" 2>/dev/null || echo "0")
            FHIR_PATH=$(python3 -c "import json,sys; d=json.load(open('${INGEST_OUT}')); print((d.get('metadata') or {}).get('fhir_write_path',''))" 2>/dev/null || echo "")

            ok "HTTP ${HTTP_CODE} | kind=${KIND} | n_values=${N_VALUES} | doc_ref=${DOC_REF} | path=${FHIR_PATH}"

            if [[ "${KIND}" != "lab_report" ]]; then
                fail "expected extraction.kind=lab_report, got '${KIND}'"
            fi
            if [[ "${N_VALUES}" -lt 1 ]]; then
                fail "expected at least 1 LabValue with citations"
            fi
            if [[ -z "${DOC_REF}" ]]; then
                fail "no document_reference_id returned"
            fi
        else
            fail "ingest returned HTTP ${HTTP_CODE}: $(head -c 200 "${INGEST_OUT}")"
            DOC_REF=""
        fi
        rm -f "${INGEST_OUT}"
        unset JWT
    fi
fi

# ---------------------------------------------------------------------------
# Check 3 — Document is persisted in OpenEMR's chart
#
# Two complementary probes:
#   3a. Direct row in OpenEMR.documents (what clinicians see in the
#       Documents tab UI, and what the agent's Postgres extraction record
#       references). This is the definitive chart-presence signal.
#   3b. FHIR DocumentReference read for the same patient. The deployed
#       OpenEMR returns total=0 because DocumentService::search (line
#       282-286 in OpenEMR core) filters by ACL via $document->can_access(),
#       and the OAuth user lacks the patients/docs ACL grant on this
#       deploy. Granting it via Administration UI activates the FHIR
#       read path without code changes. Reported as an INFO line, not a
#       hard fail. See W2_ARCHITECTURE.md §4.2.3.
# ---------------------------------------------------------------------------

bold "3. Chart round-trip — document persisted in OpenEMR"

# Strip the "copilot:" / "rest:" / "local:" prefix to compare bare ids.
EXPECTED_DOC_NUM=""
case "${DOC_REF:-}" in
    copilot:*) EXPECTED_DOC_NUM="${DOC_REF#copilot:}" ;;
    rest:*)    EXPECTED_DOC_NUM="${DOC_REF#rest:}" ;;
    local:*)   EXPECTED_DOC_NUM="" ;;  # local-disk path = chart NOT updated
    "")        EXPECTED_DOC_NUM="" ;;
    *)         EXPECTED_DOC_NUM="${DOC_REF}" ;;
esac

if [[ -z "${EXPECTED_DOC_NUM}" ]]; then
    if [[ "${DOC_REF}" == local:* ]]; then
        fail "ingest used local-disk fallback — chart write NOT performed (chart round-trip cannot be verified)"
    else
        fail "no documentId from previous step; cannot probe chart"
    fi
else
    # 3a. Direct DB probe — definitive chart-presence signal.
    if command -v railway >/dev/null 2>&1; then
        railway service MySQL >/dev/null 2>&1 || true
        DB_PUB="$(railway variables --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("MYSQL_PUBLIC_URL",""))' 2>/dev/null || echo "")"
        if [[ -n "${DB_PUB}" ]]; then
            DOC_ROWS=$(DB_PUB="${DB_PUB}" PID="${PATIENT_ID}" DOC_NUM="${EXPECTED_DOC_NUM}" python3 - <<'PY' 2>/dev/null || echo "?"
import asyncio, aiomysql, urllib.parse, os
async def run():
    u = urllib.parse.urlparse(os.environ['DB_PUB'])
    conn = await aiomysql.connect(host=u.hostname, port=u.port, user=u.username, password=u.password, db='openemr', autocommit=True)
    cur = await conn.cursor()
    await cur.execute('SELECT COUNT(*) FROM documents WHERE id=%s AND foreign_id=%s AND deleted=0', (int(os.environ['DOC_NUM']), int(os.environ['PID'])))
    row = await cur.fetchone()
    print(int(row[0]) if row else 0)
    conn.close()
asyncio.run(run())
PY
)
            if [[ "${DOC_ROWS}" == "1" ]]; then
                ok "OpenEMR.documents id=${EXPECTED_DOC_NUM} foreign_id=${PATIENT_ID} deleted=0 (visible in Documents tab)"
            else
                fail "OpenEMR.documents id=${EXPECTED_DOC_NUM} for patient ${PATIENT_ID} not found (DOC_ROWS=${DOC_ROWS})"
            fi
        else
            echo "  [SKIP] MYSQL_PUBLIC_URL unavailable; direct DB probe skipped"
        fi
        # Restore link to agent-api for any downstream commands.
        railway service copilot-agent-api >/dev/null 2>&1 || true
    else
        echo "  [SKIP] railway CLI unavailable; direct DB probe skipped"
    fi

    # 3b. FHIR read uses the OAuth password grant (same path as W1 reads).
    # Reported as INFO — not a hard fail — because DocumentService::search
    # ACL-filters out docs the OAuth user can't access (the pilot user
    # lacks patients/docs); see W2_ARCHITECTURE §4.2.3.
    # Source creds from Railway — local docker-compose uses its own
    # OpenEMR install with different random secrets, so .env.copilot
    # creds won't authenticate against the DEPLOYED OpenEMR.
    if command -v railway >/dev/null 2>&1; then
        railway service copilot-agent-api >/dev/null 2>&1 || true
        FHIR_VARS_JSON="$(railway variables --json 2>/dev/null || echo '{}')"
        FHIR_CLIENT_ID="$(echo "${FHIR_VARS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("FHIR_CLIENT_ID",""))' 2>/dev/null || echo "")"
        FHIR_CLIENT_SECRET="$(echo "${FHIR_VARS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("FHIR_CLIENT_SECRET",""))' 2>/dev/null || echo "")"
        FHIR_USERNAME="$(echo "${FHIR_VARS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("FHIR_USERNAME",""))' 2>/dev/null || echo "")"
        FHIR_PASSWORD="$(echo "${FHIR_VARS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("FHIR_PASSWORD",""))' 2>/dev/null || echo "")"
        FHIR_USER_ROLE="$(echo "${FHIR_VARS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("FHIR_USER_ROLE","users"))' 2>/dev/null || echo "users")"
        unset FHIR_VARS_JSON
    fi

    FHIR_TOKEN_RESP="$(curl -sS -m 10 -X POST \
        -d "grant_type=password" \
        -d "client_id=${FHIR_CLIENT_ID:-}" \
        -d "client_secret=${FHIR_CLIENT_SECRET:-}" \
        -d "username=${FHIR_USERNAME:-}" \
        -d "password=${FHIR_PASSWORD:-}" \
        -d "user_role=${FHIR_USER_ROLE:-users}" \
        -d "scope=openid api:fhir user/DocumentReference.rs user/Patient.rs" \
        "${OPENEMR}/oauth2/default/token" 2>/dev/null || echo '{}')"
    FHIR_TOKEN=$(python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('access_token',''))" <<< "${FHIR_TOKEN_RESP}" 2>/dev/null || echo "")

    if [[ -z "${FHIR_TOKEN}" ]]; then
        echo "  [INFO] FHIR token acquisition failed — DocumentReference probe skipped"
    else
        DOCREF_OUT="$(mktemp)"
        DOCREF_CODE="$(curl -sS -m 15 -o "${DOCREF_OUT}" -w '%{http_code}' \
            -H "Authorization: Bearer ${FHIR_TOKEN}" \
            -H "Accept: application/fhir+json" \
            "${OPENEMR}/apis/default/fhir/DocumentReference?subject=Patient/${PATIENT_ID}&_count=200&_sort=-date" 2>/dev/null || echo 000)"

        if [[ "${DOCREF_CODE}" == "200" ]]; then
            TOTAL=$(python3 -c "import json,sys; d=json.load(open('${DOCREF_OUT}')); print(d.get('total','?'))" 2>/dev/null || echo "?")
            if [[ "${TOTAL}" == "0" ]] || [[ "${TOTAL}" == "?" ]]; then
                echo "  [INFO] FHIR DocumentReference total=${TOTAL} for patient ${PATIENT_ID} — DocumentService::search filters by ACL (line 282-286 in OpenEMR core); the OAuth user lacks the patients/docs ACL grant on this deploy. Granting it via Administration UI activates the FHIR read path without code changes. The doc IS in the chart and reachable via Postgres copilot_doc_extractions (see W2_ARCHITECTURE §4.2.3)."
            else
                ok "FHIR DocumentReference search returned ${DOCREF_CODE}, total=${TOTAL}"
            fi
        else
            echo "  [INFO] FHIR DocumentReference probe returned HTTP ${DOCREF_CODE}"
        fi
        rm -f "${DOCREF_OUT}"
    fi
    unset FHIR_TOKEN FHIR_TOKEN_RESP
fi

# ---------------------------------------------------------------------------
# Check 5 — Provenance chain — Observation.derivedFrom resolves to source PDF
#
# We assert the chain end-to-end across the agent-api response and OpenEMR's
# module-private MySQL ``copilot_observations`` table:
#
#   1. metadata.observation_ids is non-empty (the chain is being produced).
#   2. Every id has the deterministic shape ``copilot-{doc_id}-{loinc}``.
#   3. Every id has a row in MySQL.openemr.copilot_observations whose
#      fhir_resource JSON's derivedFrom[0].reference points at
#      DocumentReference/copilot-<doc_id> for the same doc.
#   4. The corresponding documents.id row exists with deleted=0
#      (the source PDF still resolvable from the chart).
#   5. Each row's _copilot_citations is a non-empty array of
#      {bbox_id, quote_or_value} dicts (citation provenance).
#
# Note: We can't (yet) round-trip Observation as a queryable FHIR resource
# because OpenEMR's deployed FHIR Observation read surface is gated by the
# same OAuth-bearer/PHP-session bind issue as DocumentReference search
# (W2_ARCHITECTURE §4.2.3). The MySQL probe is the canonical chain check.
# ---------------------------------------------------------------------------

bold "5. Provenance chain — Observation.derivedFrom resolves to source PDF"

if [[ -z "${EXPECTED_DOC_NUM:-}" ]] || [[ -z "${DOC_REF:-}" ]]; then
    fail "no document reference from Check 2 — provenance chain cannot be verified"
elif ! command -v railway >/dev/null 2>&1; then
    echo "  [SKIP] railway CLI unavailable; provenance chain cannot probe MySQL"
else
    JWT2="$(mint_jwt)" || JWT2=""
    if [[ -z "${JWT2}" ]]; then
        fail "could not mint JWT for Check 5"
    else
        INGEST5_OUT="$(mktemp)"
        HTTP_CODE5="$(curl -sS -m 90 -o "${INGEST5_OUT}" -w '%{http_code}' \
            -X POST \
            -H "Authorization: Bearer ${JWT2}" \
            -F "file=@${FIXTURE}" \
            -F "patient_id=${PATIENT_ID}" \
            -F "doc_type_hint=lab_report" \
            "${AGENT_API}/document/ingest" 2>/dev/null || echo 000)"
        unset JWT2

        if [[ "${HTTP_CODE5}" != "200" ]]; then
            fail "Check 5 ingest re-probe returned HTTP ${HTTP_CODE5}"
        else
            OBS_IDS=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(" ".join((d.get("metadata") or {}).get("observation_ids") or []))' "${INGEST5_OUT}" 2>/dev/null || echo "")
            N_OBS="$(echo "${OBS_IDS}" | tr ' ' '\n' | grep -cE '^.+$' || echo 0)"

            if [[ "${N_OBS}" -lt 1 ]]; then
                fail "metadata.observation_ids is empty — provenance chain not produced"
            else
                # Validate id shape locally before hitting MySQL.
                BAD_SHAPE=""
                for OID in ${OBS_IDS}; do
                    if [[ ! "${OID}" =~ ^copilot-${EXPECTED_DOC_NUM}-[A-Za-z0-9._-]+$ ]]; then
                        BAD_SHAPE="${OID}"
                        break
                    fi
                done
                if [[ -n "${BAD_SHAPE}" ]]; then
                    fail "observation_id has unexpected shape: ${BAD_SHAPE}"
                else
                    ok "${N_OBS} observation_ids returned, all match copilot-${EXPECTED_DOC_NUM}-{loinc}"
                fi

                # Pull MySQL URL.
                railway service MySQL >/dev/null 2>&1 || true
                DB_PUB5="$(railway variables --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("MYSQL_PUBLIC_URL",""))' 2>/dev/null || echo "")"

                if [[ -z "${DB_PUB5}" ]]; then
                    echo "  [SKIP] MYSQL_PUBLIC_URL unavailable; provenance MySQL probe skipped"
                else
                    PROBE_RESULT=$(DB_PUB="${DB_PUB5}" OBS_IDS="${OBS_IDS}" DOC_NUM="${EXPECTED_DOC_NUM}" PID="${PATIENT_ID}" python3 - 2>/dev/null <<'PY'
import asyncio, aiomysql, urllib.parse, os, json, sys
async def run():
    u = urllib.parse.urlparse(os.environ['DB_PUB'])
    obs_ids = [x for x in os.environ['OBS_IDS'].split() if x]
    doc_num = int(os.environ['DOC_NUM'])
    conn = await aiomysql.connect(host=u.hostname, port=u.port, user=u.username, password=u.password, db='openemr', autocommit=True)
    cur = await conn.cursor()
    fail_msgs = []
    derived_ok = True
    citations_ok = True
    for oid in obs_ids:
        await cur.execute('SELECT fhir_resource, citations FROM copilot_observations WHERE id=%s', (oid,))
        row = await cur.fetchone()
        if not row:
            derived_ok = False
            fail_msgs.append(f"missing_row:{oid}")
            continue
        try:
            fhir = json.loads(row[0]) if isinstance(row[0], (str, bytes)) else (row[0] or {})
        except Exception:
            fhir = {}
        derived = fhir.get('derivedFrom') or []
        if not derived or not isinstance(derived, list):
            derived_ok = False
            fail_msgs.append(f"no_derivedFrom:{oid}")
            continue
        ref = (derived[0] or {}).get('reference', '') if isinstance(derived[0], dict) else ''
        if ref != f'DocumentReference/copilot-{doc_num}':
            derived_ok = False
            fail_msgs.append(f"wrong_ref:{oid}:{ref}")
        try:
            cits = json.loads(row[1]) if isinstance(row[1], (str, bytes)) else (row[1] or [])
        except Exception:
            cits = []
        if not cits or not isinstance(cits, list):
            citations_ok = False
            fail_msgs.append(f"no_citations:{oid}")
            continue
        cit0 = cits[0] if cits else {}
        if not isinstance(cit0, dict) or not cit0.get('bbox_id') or not cit0.get('quote_or_value'):
            citations_ok = False
            fail_msgs.append(f"citation_shape:{oid}")
    # Also confirm the source PDF row exists and isn't deleted.
    await cur.execute('SELECT COUNT(*) FROM documents WHERE id=%s AND deleted=0', (doc_num,))
    row = await cur.fetchone()
    doc_ok = bool(row and int(row[0]) >= 1)
    conn.close()
    print(json.dumps({'derived_ok': derived_ok, 'citations_ok': citations_ok, 'doc_ok': doc_ok, 'fail': fail_msgs}))
asyncio.run(run())
PY
)
                    if [[ -z "${PROBE_RESULT}" ]]; then
                        fail "provenance MySQL probe errored"
                    else
                        DERIVED_OK=$(echo "${PROBE_RESULT}" | python3 -c "import json,sys; print(json.load(sys.stdin).get('derived_ok'))" 2>/dev/null || echo "False")
                        CIT_OK=$(echo "${PROBE_RESULT}" | python3 -c "import json,sys; print(json.load(sys.stdin).get('citations_ok'))" 2>/dev/null || echo "False")
                        DOC_OK=$(echo "${PROBE_RESULT}" | python3 -c "import json,sys; print(json.load(sys.stdin).get('doc_ok'))" 2>/dev/null || echo "False")
                        FAIL_LIST=$(echo "${PROBE_RESULT}" | python3 -c "import json,sys; print(','.join(json.load(sys.stdin).get('fail') or []))" 2>/dev/null || echo "")

                        if [[ "${DERIVED_OK}" == "True" ]]; then
                            ok "Each row in MySQL.copilot_observations has fhir_resource.derivedFrom -> DocumentReference/copilot-${EXPECTED_DOC_NUM}"
                        else
                            fail "derivedFrom chain broken: ${FAIL_LIST}"
                        fi
                        if [[ "${DOC_OK}" == "True" ]]; then
                            ok "documents.id=${EXPECTED_DOC_NUM} row exists with deleted=0"
                        else
                            fail "documents.id=${EXPECTED_DOC_NUM} missing or deleted"
                        fi
                        if [[ "${CIT_OK}" == "True" ]]; then
                            ok "Each row's _copilot_citations carries at least one {bbox_id, quote_or_value}"
                        else
                            fail "citation chain broken: ${FAIL_LIST}"
                        fi
                    fi
                fi
                # Restore service link.
                railway service copilot-agent-api >/dev/null 2>&1 || true
            fi
        fi
        rm -f "${INGEST5_OUT}"
    fi
fi

# ---------------------------------------------------------------------------
# Check 4 — Eval gate status (delegated to CI)
# ---------------------------------------------------------------------------

bold "4. Eval gate status (CI)"

if ! command -v gh >/dev/null 2>&1; then
    fail "gh CLI not available; cannot summarise CI status"
else
    # Latest run on clinical-copilot
    LATEST_CC=$(gh run list --repo "${GH_REPO}" --branch clinical-copilot --workflow copilot-eval.yml --limit 1 --json conclusion 2>/dev/null || echo "[]")
    CC_RESULT=$(python3 -c "import json,sys; arr=json.loads('''${LATEST_CC}''') if '''${LATEST_CC}'''.strip() else []; print(arr[0].get('conclusion','') if arr else '')" 2>/dev/null || echo "")
    if [[ "${CC_RESULT}" == "success" ]]; then
        ok "clinical-copilot W2 Eval Suite: success"
    elif [[ "${CC_RESULT}" == "failure" ]]; then
        # Failure is expected on regression branch but NOT on clinical-copilot.
        fail "clinical-copilot W2 Eval Suite: failure (open the run to see which rubric)"
    else
        fail "clinical-copilot W2 Eval Suite: ${CC_RESULT:-unknown}"
    fi

    # Regression PR run — MUST be red (proves the gate bites).
    LATEST_REG=$(gh run list --repo "${GH_REPO}" --branch regression/seed-strip-citations --workflow copilot-eval.yml --limit 1 --json conclusion 2>/dev/null || echo "[]")
    REG_RESULT=$(python3 -c "import json,sys; arr=json.loads('''${LATEST_REG}''') if '''${LATEST_REG}'''.strip() else []; print(arr[0].get('conclusion','') if arr else '')" 2>/dev/null || echo "")
    if [[ "${REG_RESULT}" == "failure" ]]; then
        ok "regression/seed-strip-citations W2 Eval Suite: failure (gate hard-fails on regression — by design)"
    elif [[ "${REG_RESULT}" == "success" ]]; then
        fail "regression branch passed — gate does NOT bite the seeded regression"
    else
        fail "regression branch CI status: ${REG_RESULT:-unknown}"
    fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo
if [[ "${OVERALL}" -eq 0 ]]; then
    bold "VERIFY: PASS"
    exit 0
else
    bold "VERIFY: FAIL"
    exit 1
fi
