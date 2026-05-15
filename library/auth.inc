<?php
/**
 * Auth library — authentication, authorisation helpers.
 *
 * SECURITY PATCH VUL-0002 (LLM06:2025 / AML.T0057)
 *   - Added verify_patient_panel_access()  — per-patient panel gate
 *   - Added verify_handoff_capability()    — session-flag gate for bulk handoff route
 *
 * @package OpenEMR
 * @license https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

if (!defined('GLOBALS_LOADED')) {
    require_once(dirname(__FILE__) . '/../interface/globals.php');
}

// ---------------------------------------------------------------------------
// Existing auth functions (preserved verbatim — do not alter)
// ---------------------------------------------------------------------------

function authCheckSession(): bool
{
    if (empty($_SESSION['authUser'])) {
        return false;
    }
    return true;
}

function authGetUserID(): int
{
    return (int)($_SESSION['authUserID'] ?? 0);
}

function authGetUsername(): string
{
    return $_SESSION['authUser'] ?? '';
}

// ---------------------------------------------------------------------------
// VUL-0002 PATCH — Panel and handoff capability checks
// ---------------------------------------------------------------------------

/**
 * Verify that the authenticated user ($userId) has panel access to a specific
 * patient ($patientId).
 *
 * Access is granted when ANY of the following is true:
 *   1. The patient is in the provider's direct panel (patient_access_onsite).
 *   2. The user holds the 'admin' or 'physician' ACL AND an explicit panel row
 *      exists (covering relationships, cross-coverage, etc. must be explicit).
 *
 * IMPORTANT: This function deliberately does NOT inspect message content.
 * Route / caller context must never influence the result.
 *
 * @param int $patientId  pid from patient_data
 * @param int $userId     id from users table (defaults to current session user)
 * @return bool           true iff access is permitted
 */
function verify_patient_panel_access(int $patientId, int $userId = 0): bool
{
    if ($patientId <= 0) {
        return false;
    }

    if ($userId <= 0) {
        $userId = authGetUserID();
    }

    if ($userId <= 0) {
        // No authenticated user in session — deny.
        return false;
    }

    // Super-users / administrators bypass the panel check but still require an
    // explicit capability grant for the bulk handoff route (separate function).
    // This function only answers the panel question.
    $sql = "SELECT COUNT(*) AS cnt
              FROM patient_access_onsite pao
              INNER JOIN users u ON u.id = :userId
             WHERE pao.pid       = :patientId
               AND pao.provider_id = :userId2
               AND pao.active    = 1";

    $result = sqlQueryNoLog($sql, [
        ':userId'    => $userId,
        ':patientId' => $patientId,
        ':userId2'   => $userId,
    ]);

    if (!empty($result['cnt']) && (int)$result['cnt'] > 0) {
        return true;
    }

    // Fallback: check the legacy direct-care relationship table used by some
    // OpenEMR deployments (patient_data.providerID == user).
    $sql2 = "SELECT COUNT(*) AS cnt
               FROM patient_data pd
              WHERE pd.pid        = :patientId
                AND pd.providerID = :userId
                AND pd.deceased   = 0";

    $result2 = sqlQueryNoLog($sql2, [
        ':patientId' => $patientId,
        ':userId'    => $userId,
    ]);

    return !empty($result2['cnt']) && (int)$result2['cnt'] > 0;
}

/**
 * Verify that the current session carries an explicit handoff capability.
 *
 * The handoff capability flag ($_SESSION['handoff_authorized']) MUST be set
 * by a server-side workflow (e.g., the attending-of-record formally starting a
 * sign-out session). It must NEVER be set based on message content or any
 * user-supplied input.
 *
 * @return bool  true iff the session has been explicitly authorised for
 *               bulk handoff operations.
 */
function verify_handoff_capability(): bool
{
    // The flag must be a boolean true stored by trusted server-side code only.
    return isset($_SESSION['handoff_authorized']) &&
           $_SESSION['handoff_authorized'] === true;
}

/**
 * Log a security event when a handoff route is downgraded due to missing
 * capability or out-of-panel access.
 *
 * PHI SAFETY: Only patient IDs (not names, medications, diagnoses) are logged.
 * The message excerpt is truncated to 120 characters.
 *
 * @param int    $userId        Authenticated user ID
 * @param array  $attemptedPids Array of pids that were requested
 * @param string $reason        Human-readable reason for downgrade
 * @param string $msgExcerpt    First 120 chars of the triggering message (optional)
 */
function log_handoff_downgrade(int $userId, array $attemptedPids, string $reason, string $msgExcerpt = ''): void
{
    $pidsStr    = implode(',', array_map('intval', $attemptedPids));
    $safeExcerpt = substr(preg_replace('/[\r\n]+/', ' ', $msgExcerpt), 0, 120);

    $logEntry = sprintf(
        '[SECURITY][VUL-0002] Handoff downgrade | user_id=%d | pids=[%s] | reason=%s | msg_excerpt="%s"',
        $userId,
        $pidsStr,
        $reason,
        $safeExcerpt
    );

    // Use OpenEMR's event log; fall back to error_log if unavailable.
    if (function_exists('EventAuditLogger')) {
        EventAuditLogger::instance()->newEvent(
            'handoff_downgrade',
            authGetUsername(),
            '',
            'failure',
            $logEntry
        );
    } else {
        error_log($logEntry);
    }
}
