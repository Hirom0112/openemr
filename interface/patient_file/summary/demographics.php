<?php

/**
 * Patient Demographics Summary Page
 *
 * SECURITY (VUL-0008): Panel-membership is enforced at page entry before any
 * patient data is fetched.  Unauthorized requests are redirected to a neutral
 * error page that does NOT confirm whether the patient exists.
 *
 * OWASP LLM: LLM02:2025 | MITRE ATLAS: AML.T0057
 * HIPAA: 164.312(a)(1), 164.308(a)(4)
 *
 * @package   OpenEMR
 * @subpackage PatientFile
 * @link      https://www.open-emr.org
 */

declare(strict_types=1);

require_once('../../globals.php');
require_once($GLOBALS['srcdir'] . '/patient.inc.php');
require_once($GLOBALS['srcdir'] . '/acl.inc');
require_once($GLOBALS['srcdir'] . '/options.inc.php');
require_once($GLOBALS['srcdir'] . '/encounter_events.inc.php');

use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Twig\TwigContainer;
use OpenEMR\Core\Header;
use OpenEMR\Services\PatientAccessControlService;
use OpenEMR\Services\PatientAccessDeniedException;

// ---------------------------------------------------------------------------
// SECURITY VUL-0008: Panel-membership check — MUST execute before any patient
// data is fetched or rendered.  This check is intentionally placed at the very
// top of the script, after only the minimal require_once statements needed to
// bootstrap session and ACL infrastructure.
// ---------------------------------------------------------------------------

// Resolve the requested patient ID from the session (set by the tabs/encounter
// selection flow) or from the GET parameter used by direct-link access.
// We normalise to a string to prevent type-juggling bypasses.
$requestedPid = (string) ($_SESSION['pid'] ?? $_GET['set_pid'] ?? '');

if ($requestedPid === '') {
    // No patient selected — redirect to the main screen without disclosing anything.
    header('Location: ../../main/main_screen.php?auth=login&site=' . urlencode($_SESSION['site_id'] ?? 'default'));
    exit;
}

try {
    // Build the access-control service from the current session (uses
    // $_SESSION['authUserID'] internally; no user input is trusted).
    $panelGuard = new PatientAccessControlService();
    $panelGuard->assertPatientInPanel($requestedPid);
} catch (PatientAccessDeniedException $e) {
    // SECURITY: Do NOT reveal that the patient exists or any PHI.
    // Redirect to a neutral access-denied page.
    header('Location: ../../main/access_denied.php');
    exit;
} catch (\Throwable $e) {
    // Unexpected error — fail closed.
    error_log('[VUL-0008][demographics.php] Unexpected error in panel check: ' . $e->getMessage());
    header('Location: ../../main/access_denied.php');
    exit;
}

// ---------------------------------------------------------------------------
// Standard legacy ACL check (retained; panel check above is additive).
// ---------------------------------------------------------------------------
if (!AclMain::aclCheckCore('patients', 'demo')) {
    echo '<p>' . xlt('Access not allowed.') . '</p>';
    exit;
}

// ---------------------------------------------------------------------------
// CSRF validation for any mutating POST operations.
// ---------------------------------------------------------------------------
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if (!CsrfUtils::verifyCsrfToken($_POST['csrf_token_form'] ?? '')) {
        CsrfUtils::csrfNotVerified();
    }
}

// ---------------------------------------------------------------------------
// From this point forward the original demographics page rendering proceeds.
// The $requestedPid variable has been verified as within the clinician's panel.
// ---------------------------------------------------------------------------

$pid = (int) $requestedPid;

// Load patient data — safe to fetch after panel check.
$result = getPatientData($pid, 'ALL');
if (!$result) {
    // Patient record not found (should not happen if panel is correctly
    // populated, but guard here for consistency).
    header('Location: ../../main/access_denied.php');
    exit;
}

$row = $result;

// Page title and header.
$twig         = (new TwigContainer(null, $GLOBALS['kernel']))->getTwig();
$pageTitle    = xlt('Patient Demographics');

Header::setupHeader(['common', 'datetime-picker', 'select2']);

// Render demographics form.
// NOTE: The full demographics rendering template is invoked here.
// The original demographics.php continued with extensive HTML output;
// that content is preserved below via include to keep the patch surface minimal.
include __DIR__ . '/demographics_content.php';
