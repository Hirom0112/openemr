#!/usr/bin/env bash
# =============================================================================
# 05-enable-openemr-module.sh
#
# Enables the oe-module-clinical-copilot module in the Railway OpenEMR instance
# by inserting/updating the module record in the database directly via MySQL.
#
# OpenEMR's module manager stores enabled modules in the `modules` table.
# This script activates the module without requiring browser interaction.
#
# Usage:
#   export MYSQL_ROOT_PASS=XFmmPXWfRBhYVusWhQwhqhfXrBqCOETP
#   export MYSQL_HOST=shuttle.proxy.rlwy.net
#   export MYSQL_PORT=<tcp-proxy-port-from-railway-mysql-service>
#   ./scripts/05-enable-openemr-module.sh
#
# Find MYSQL_HOST and MYSQL_PORT in Railway: MySQL service → Connect tab →
# "Public Networking" section.
# =============================================================================

set -euo pipefail

MYSQL_HOST="${MYSQL_HOST:-shuttle.proxy.rlwy.net}"
MYSQL_PORT="${MYSQL_PORT:-}"
MYSQL_USER="${MYSQL_USER:-root}"
MYSQL_PASS="${MYSQL_PASS:-${MYSQL_ROOT_PASS:-}}"
MYSQL_DB="${MYSQL_DB:-openemr}"
MODULE_NAME="oe-module-clinical-copilot"

if [[ -z "${MYSQL_PORT}" ]]; then
  echo "ERROR: MYSQL_PORT is not set."
  echo "Find it in Railway: MySQL service → Connect → Public Networking"
  exit 1
fi
if [[ -z "${MYSQL_PASS}" ]]; then
  echo "ERROR: MYSQL_PASS or MYSQL_ROOT_PASS is not set."
  exit 1
fi

if ! command -v mysql &>/dev/null; then
  echo "ERROR: mysql client not installed."
  echo "Install: brew install mysql-client (Mac) or apt-get install mysql-client (Linux)"
  exit 1
fi

echo "==> Connecting to MySQL at ${MYSQL_HOST}:${MYSQL_PORT}"

mysql -h "${MYSQL_HOST}" -P "${MYSQL_PORT}" -u "${MYSQL_USER}" -p"${MYSQL_PASS}" "${MYSQL_DB}" <<'SQL'
-- Enable the Clinical Co-Pilot module in OpenEMR's module registry.
-- OpenEMR checks the `modules` table to determine which modules are active.
INSERT INTO modules (mod_name, mod_ui_name, mod_directory, mod_ui_active, mod_active, date)
VALUES (
  'oe-module-clinical-copilot',
  'Clinical Co-Pilot',
  'oe-module-clinical-copilot',
  1,
  1,
  NOW()
)
ON DUPLICATE KEY UPDATE
  mod_ui_active = 1,
  mod_active    = 1;

SELECT mod_name, mod_ui_name, mod_active, mod_ui_active
FROM modules
WHERE mod_name = 'oe-module-clinical-copilot';
SQL

echo ""
echo "==> Module enabled. Navigate to OpenEMR and verify the Co-Pilot panel loads."
echo "    URL: https://your-openemr-service.up.railway.app"
echo ""
echo "    If the panel doesn't appear, go to:"
echo "    Admin → Modules → Manage Modules → find Clinical Co-Pilot → Enable"
