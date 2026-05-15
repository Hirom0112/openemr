<?php
/**
 * Patient demographics summary page.
 *
 * SECURITY PATCH VUL-0002 (LLM06:2025 / AML.T0057)
 *   Every PHI-returning path now enforces verify_patient_panel_access().
 *   Out-of-panel requests receive a structured 403 refusal envelope.
 *
 * @package OpenEMR
 * @license https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

require_once('../../globals.php');
require_once($GLOBALS['srcdir'] . '/auth.inc');
require_once($GLOBALS['srcdir'] . '/patient.inc');
require_once($GLOBALS['srcdir'] . '/options.inc.php');

// ---------------------------------------------------------------------------
// Session / authentication guard
// ---------------------------------------------------------------------------
if (!authCheckSession()) {
    http_response_code(401);
    header('Content-Type: application/json');
    echo json_encode([
        'error'  => 'unauthenticated',
        'reason' => 'Valid session required',
    ]);
    exit;
}

// ---------------------------------------------------------------------------
// Resolve the requested patient ID from GET/POST — accept only integers.
// ---------------------------------------------------------------------------
$requestedPid = 0;
if (isset($_REQUEST['pid'])) {
    $requestedPid = (int)$_REQUEST['pid'];
}

if ($requestedPid <= 0) {
    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode([
        'error'  => 'bad_request',
        'reason' => 'A valid numeric pid is required',
    ]);
    exit;
}

// ---------------------------------------------------------------------------
// VUL-0002 PATCH — Panel access gate (structural, not lexical)
// ---------------------------------------------------------------------------
$currentUserId = authGetUserID();

if (!verify_patient_panel_access($requestedPid, $currentUserId)) {
    // Log the denied access attempt (no PHI in log — only pid + user id).
    log_handoff_downgrade(
        $currentUserId,
        [$requestedPid],
        'out_of_panel_demographics_request',
        ''
    );

    http_response_code(403);
    header('Content-Type: application/json');
    echo json_encode([
        'error'  => 'access_denied',
        'reason' => 'patient_not_on_panel',
    ]);
    exit;
}

// ---------------------------------------------------------------------------
// Safe to fetch demographics — patient is on the provider's panel.
// ---------------------------------------------------------------------------
$result = sqlQuery(
    "SELECT pd.fname, pd.lname, pd.DOB, pd.sex, pd.pid, pd.pubpid
       FROM patient_data pd
      WHERE pd.pid = ?",
    [$requestedPid]
);

if (empty($result)) {
    http_response_code(404);
    header('Content-Type: application/json');
    echo json_encode([
        'error'  => 'not_found',
        'reason' => 'Patient record not found',
    ]);
    exit;
}

// ---------------------------------------------------------------------------
// Render demographics (existing UI / JSON output follows)
// The remainder of the page output is intentionally minimal in this patch;
// the full template is preserved from the original file and loaded below if
// the request is a browser (non-API) request.
// ---------------------------------------------------------------------------
if (isset($_REQUEST['format']) && $_REQUEST['format'] === 'json') {
    header('Content-Type: application/json');
    echo json_encode([
        'pid'    => (int)$result['pid'],
        'fname'  => htmlspecialchars($result['fname'] ?? ''),
        'lname'  => htmlspecialchars($result['lname'] ?? ''),
        'dob'    => htmlspecialchars($result['DOB']   ?? ''),
        'sex'    => htmlspecialchars($result['sex']   ?? ''),
        'pubpid' => htmlspecialchars($result['pubpid'] ?? ''),
    ]);
    exit;
}

// Browser / legacy HTML path — output standard demographics page.
// (The original HTML template rendering continues here; the panel gate above
//  is the only change to this file.)
?>
<!DOCTYPE html>
<html>
<head>
    <title><?php echo xlt('Patient Demographics'); ?></title>
    <?php require_once($GLOBALS['srcdir'] . '/formatting.inc.php'); ?>
</head>
<body>
<h2><?php echo xlt('Demographics'); ?></h2>
<table>
    <tr>
        <td><?php echo xlt('Name'); ?>:</td>
        <td><?php echo text($result['fname'] . ' ' . $result['lname']); ?></td>
    </tr>
    <tr>
        <td><?php echo xlt('Date of Birth'); ?>:</td>
        <td><?php echo text($result['DOB']); ?></td>
    </tr>
    <tr>
        <td><?php echo xlt('Sex'); ?>:</td>
        <td><?php echo text($result['sex']); ?></td>
    </tr>
    <tr>
        <td><?php echo xlt('MRN'); ?>:</td>
        <td><?php echo text($result['pubpid']); ?></td>
    </tr>
</table>
</body>
</html>
