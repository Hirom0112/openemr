<?php
/**
 * Left navigation / route dispatcher.
 *
 * SECURITY PATCH VUL-0002 (LLM06:2025 / AML.T0057)
 *   - Bulk handoff route now requires explicit session capability.
 *   - Capability is NEVER derived from message content or URL parameters.
 *   - Requests lacking the capability are downgraded to single-patient
 *     briefing route; out-of-panel patients are refused with 403.
 *
 * @package OpenEMR
 * @license https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

require_once('../globals.php');
require_once($GLOBALS['srcdir'] . '/auth.inc');

if (!authCheckSession()) {
    http_response_code(401);
    exit;
}

$currentUserId = authGetUserID();

// ---------------------------------------------------------------------------
// Route resolution
// ---------------------------------------------------------------------------
// The route name comes from a trusted internal dispatcher, never from raw
// user message text. The Co-Pilot NLU layer maps intents to route names
// server-side; only the resolved route name arrives here.
// ---------------------------------------------------------------------------
$route = $_REQUEST['route'] ?? 'briefing';

// Allowlist of valid routes to prevent arbitrary dispatch.
$allowedRoutes = ['briefing', 'handoff', 'demographics', 'medications'];
if (!in_array($route, $allowedRoutes, true)) {
    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'invalid_route']);
    exit;
}

// ---------------------------------------------------------------------------
// VUL-0002 PATCH — Handoff route capability gate
//
// The handoff route triggers bulk multi-patient data retrieval. It MUST only
// be dispatched when the session explicitly carries the handoff_authorized
// flag. This flag is set exclusively by the server-side attending sign-out
// workflow — it is never derived from message content.
// ---------------------------------------------------------------------------
if ($route === 'handoff') {
    if (!verify_handoff_capability()) {
        // Downgrade: treat as a single-patient briefing.
        // Determine which patient was requested (if any).
        $requestedPid = isset($_REQUEST['pid']) ? (int)$_REQUEST['pid'] : 0;

        log_handoff_downgrade(
            $currentUserId,
            $requestedPid > 0 ? [$requestedPid] : [],
            'missing_handoff_capability_flag',
            // Do NOT log raw message text — log only the resolved route.
            'route=handoff downgraded to briefing'
        );

        // Redirect internally to single-patient briefing.
        $route = 'briefing';
    }
}

// ---------------------------------------------------------------------------
// For any route that accesses patient data, enforce per-patient panel check.
// ---------------------------------------------------------------------------
if (in_array($route, ['briefing', 'medications', 'demographics'], true)) {
    $requestedPid = isset($_REQUEST['pid']) ? (int)$_REQUEST['pid'] : 0;

    if ($requestedPid > 0 && !verify_patient_panel_access($requestedPid, $currentUserId)) {
        log_handoff_downgrade(
            $currentUserId,
            [$requestedPid],
            'out_of_panel_' . $route . '_request',
            'route=' . $route
        );

        http_response_code(403);
        header('Content-Type: application/json');
        echo json_encode([
            'error'  => 'access_denied',
            'reason' => 'patient_not_on_panel',
        ]);
        exit;
    }
}

// ---------------------------------------------------------------------------
// For bulk handoff (when capability IS present), verify every requested
// patient is on the provider's panel before returning any data.
// ---------------------------------------------------------------------------
if ($route === 'handoff' && verify_handoff_capability()) {
    $requestedPids = [];

    // Bulk pid list may arrive as comma-separated string or JSON array.
    if (isset($_REQUEST['pids'])) {
        $rawPids = $_REQUEST['pids'];
        if (is_string($rawPids)) {
            // Accept both comma-separated and JSON-encoded arrays.
            if ($rawPids[0] === '[') {
                $decoded = json_decode($rawPids, true);
                $requestedPids = is_array($decoded) ? $decoded : [];
            } else {
                $requestedPids = explode(',', $rawPids);
            }
        } elseif (is_array($rawPids)) {
            $requestedPids = $rawPids;
        }
    }

    // Normalise to ints and remove zeros.
    $requestedPids = array_filter(array_map('intval', $requestedPids));

    $deniedPids = [];
    foreach ($requestedPids as $pid) {
        if (!verify_patient_panel_access($pid, $currentUserId)) {
            $deniedPids[] = $pid;
        }
    }

    if (!empty($deniedPids)) {
        log_handoff_downgrade(
            $currentUserId,
            $deniedPids,
            'out_of_panel_patients_in_bulk_handoff',
            'route=handoff bulk'
        );

        // Refuse the entire bulk request — do not return a partial list,
        // which could still leak the fact that certain patients are admitted.
        http_response_code(403);
        header('Content-Type: application/json');
        echo json_encode([
            'error'   => 'access_denied',
            'reason'  => 'one_or_more_patients_not_on_panel',
            // Return the count only, not the pids, to avoid enumeration.
            'denied_count' => count($deniedPids),
        ]);
        exit;
    }
}

// ---------------------------------------------------------------------------
// Dispatch to the appropriate sub-handler (existing logic preserved).
// ---------------------------------------------------------------------------
switch ($route) {
    case 'handoff':
        require_once('../copilot/handoff_route.php');
        break;

    case 'briefing':
        require_once('../copilot/briefing_route.php');
        break;

    case 'medications':
        require_once('../../patient_file/medications/list.php');
        break;

    case 'demographics':
        require_once('../../patient_file/summary/demographics.php');
        break;

    default:
        http_response_code(400);
        header('Content-Type: application/json');
        echo json_encode(['error' => 'unhandled_route']);
        exit;
}
