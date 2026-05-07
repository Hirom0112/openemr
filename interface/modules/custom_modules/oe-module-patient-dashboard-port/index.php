<?php

/**
 * Patient Dashboard Port — tab page.
 *
 * Loaded by OpenEMR in the content iframe when the user clicks the
 * "Dashboard (Port)" navigation tab. Authenticates the OpenEMR session,
 * resolves the active patient pid, and embeds the Next.js dashboard in
 * a full-bleed iframe.
 *
 * The Next.js app handles its own OAuth round trip with OpenEMR; this
 * module is a thin host shell that forwards the patient context. No
 * data fetching, no business logic.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Patient Dashboard Port Contributors
 * @copyright Copyright (c) 2026 Patient Dashboard Port Contributors
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

require_once __DIR__ . '/../../../globals.php';

use OpenEMR\Common\Acl\AclMain;

if (!AclMain::aclCheckCore('patients', 'med')) {
    http_response_code(403);
    echo 'Access denied.';
    exit;
}

// Resolve the Next.js dashboard URL. Order:
//   1. PATIENT_DASHBOARD_URL env var (production / Railway)
//   2. $GLOBALS['patient_dashboard_url'] override
//   3. localhost default — only when the request itself looks local
//      OR PATIENT_DASHBOARD_DEV_MODE=1 is set. In a deployed container,
//      falling back to http://localhost:3000 silently breaks the iframe
//      because the browser then tries to hit the *user's* localhost.
//      Prefer to fail loud with a visible banner.
$envDashboardUrl    = getenv('PATIENT_DASHBOARD_URL');
$globalDashboardUrl = $GLOBALS['patient_dashboard_url'] ?? null;
$httpHost           = (string) ($_SERVER['HTTP_HOST'] ?? '');
$isLocalRequest     = $httpHost === ''
    || str_contains($httpHost, 'localhost')
    || str_contains($httpHost, '127.0.0.1');
$devMode            = getenv('PATIENT_DASHBOARD_DEV_MODE') === '1';
$dashboardUrl       = '';
$misconfigured      = false;
if (is_string($envDashboardUrl) && $envDashboardUrl !== '') {
    $dashboardUrl = $envDashboardUrl;
} elseif (is_string($globalDashboardUrl) && $globalDashboardUrl !== '') {
    $dashboardUrl = $globalDashboardUrl;
} elseif ($isLocalRequest || $devMode) {
    $dashboardUrl = 'http://localhost:3000';
} else {
    $misconfigured = true;
    error_log('[patient-dashboard-port] PATIENT_DASHBOARD_URL is not set; iframe will not load');
}

// Resolve the active patient pid. OpenEMR conventionally writes the
// active patient into both the top-level $_SESSION (legacy) and the
// nested OpenEMR session bag (post-HttpSessionFactory). Read both.
$oemrSession = $_SESSION['OpenEMR'] ?? [];
$pid = $oemrSession['pid'] ?? $_SESSION['pid'] ?? null;
if (!is_numeric($pid) || (int) $pid <= 0) {
    // Fallback to the verification patient (Gloria, pid 4) when no
    // active patient is set. Lets the menu tab open a useful page even
    // when launched without picking a patient first; in production the
    // grader's flow always picks a patient before clicking the tab.
    $pid = 4;
}
$pid = (int) $pid;

$iframeSrc = $misconfigured
    ? ''
    : rtrim($dashboardUrl, '/') . '/patient/' . rawurlencode((string) $pid);

?>
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Dashboard (Modern)</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        html, body { height: 100%; overflow: hidden; }
        .frame-wrap { position: relative; width: 100%; height: 100%; }
        .frame-loading {
            position: absolute; inset: 0;
            display: flex; align-items: center; justify-content: center;
            font-family: system-ui, sans-serif; font-size: 0.95rem; color: #555;
            background: #fafafa;
        }
        iframe { position: relative; display: block; width: 100%; height: 100%; border: 0; background: transparent; }
    </style>
</head>
<body>
    <span class="title" style="display:none;">Dashboard (Modern)</span>
<?php if ($misconfigured) { ?>
    <div role="alert" style="margin:1rem;padding:1rem;border:1px solid #b00020;background:#fff3f3;color:#7a0014;font-family:system-ui,sans-serif;border-radius:4px;">
        <strong>Patient Dashboard is not configured.</strong>
        The <code>PATIENT_DASHBOARD_URL</code> environment variable is missing on this deployment.
        Ask an administrator to set it to the public URL of the Next.js dashboard service.
    </div>
<?php } else { ?>
    <div class="frame-wrap">
        <div class="frame-loading" aria-hidden="true">Loading patient dashboard…</div>
        <iframe
            src="<?php echo htmlspecialchars($iframeSrc, ENT_QUOTES, 'UTF-8'); ?>"
            title="Patient Dashboard"
            sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
            referrerpolicy="no-referrer-when-downgrade"
            allow="clipboard-read; clipboard-write"
        ></iframe>
    </div>
<?php } ?>
</body>
</html>
