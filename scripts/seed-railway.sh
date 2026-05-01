#!/usr/bin/env bash
# =============================================================================
# seed-railway.sh
#
# Idempotent Railway-aware entrypoint for synthetic_data/load.py.
#
# What this script does:
#   1. Validates required env vars are set.
#   2. Probes the OpenEMR MySQL database for a marker row that proves the
#      synthetic seed has already run (Marcus Webb is the canonical first
#      patient in the PATIENTS list — its presence is our idempotency check).
#   3. If already seeded, prints a log line and exits 0.
#   4. Otherwise runs synthetic_data/load.py with the env vars passed through.
#   5. Exits non-zero on any failure so Railway's deploy step fails loudly.
#
# Required env vars:
#   OE_USER         OpenEMR clinician username   (default: sara)
#   OE_PASS         OpenEMR clinician password
#   BASE_URL        Public OpenEMR URL (https://...)
#   CLIENT_ID       OAuth2 client ID for the loader
#   CLIENT_SECRET   OAuth2 client secret for the loader
#   MYSQL_HOST      MySQL host (TCP proxy host on Railway)
#   MYSQL_PORT      MySQL port (default 3306)
#   MYSQL_USER      MySQL user (default root)
#   MYSQL_PASS      MySQL password
#   MYSQL_DB        MySQL database (default openemr)
#
# Usage (manual one-shot):
#   railway run -s clinical-copilot-openemr -- ./scripts/seed-railway.sh
#
# Railway integration:
#   railway.toml does not currently support a release/deploy hook for this
#   step (Railway's deploy command runs on every restart and would re-seed
#   on every container restart even with idempotency — wasteful network).
#   Run this script manually once after the database is initialised.
# =============================================================================

set -euo pipefail

OE_USER="${OE_USER:-sara}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
MYSQL_USER="${MYSQL_USER:-root}"
MYSQL_DB="${MYSQL_DB:-openemr}"

required=(OE_PASS BASE_URL CLIENT_ID CLIENT_SECRET MYSQL_HOST MYSQL_PASS)
missing=()
for v in "${required[@]}"; do
    if [[ -z "${!v:-}" ]]; then
        missing+=("$v")
    fi
done
if (( ${#missing[@]} > 0 )); then
    echo "ERROR: missing required env vars: ${missing[*]}" >&2
    exit 2
fi

export OE_USER MYSQL_PORT MYSQL_USER MYSQL_DB

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOADER="${REPO_ROOT}/synthetic_data/load.py"

if [[ ! -f "${LOADER}" ]]; then
    echo "ERROR: loader not found at ${LOADER}" >&2
    exit 3
fi

# ── Idempotency probe ────────────────────────────────────────────────────────
# Look for the canonical first synthetic patient (Marcus Webb, DOB 1968-03-14).
# That tuple is unlikely to collide with any real patient and is created on
# every full seed run.
echo "==> Probing for prior seed (looking for Marcus Webb in patient_data)..."

probe_sql='SELECT COUNT(*) FROM patient_data WHERE fname="Marcus" AND lname="Webb" AND DOB="1968-03-14";'

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 is required for the probe and the loader" >&2
    exit 4
fi

probe_count=$(python3 - <<PY
import os, sys
try:
    import pymysql
except ImportError:
    print("pymysql not installed; run: pip3 install pymysql", file=sys.stderr)
    sys.exit(5)
conn = pymysql.connect(
    host=os.environ["MYSQL_HOST"],
    port=int(os.environ["MYSQL_PORT"]),
    user=os.environ["MYSQL_USER"],
    password=os.environ["MYSQL_PASS"],
    database=os.environ["MYSQL_DB"],
    connect_timeout=10,
)
try:
    with conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM patient_data WHERE fname=%s AND lname=%s AND DOB=%s',
                    ("Marcus", "Webb", "1968-03-14"))
        row = cur.fetchone()
        print(int(row[0]) if row else 0)
finally:
    conn.close()
PY
)

if [[ "${probe_count}" -gt 0 ]]; then
    echo "==> Database already seeded (${probe_count} marker row(s) found). Skipping load."
    exit 0
fi

echo "==> No marker row found. Running synthetic_data/load.py ..."
exec python3 "${LOADER}"
