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
$rawPid = $oemrSession['pid'] ?? $_SESSION['pid'] ?? null;
$noPatient = !is_numeric($rawPid) || (int) $rawPid <= 0;
$pid = $noPatient ? 0 : (int) $rawPid;

// Same-origin embed: load the dashboard via Apache's ProxyPass at
// /dashboard/* (configured in the Dockerfile). This puts the iframe
// at OpenEMR's own origin, sidestepping the third-party-cookie
// blocking that breaks iframe OAuth callbacks. The PATIENT_DASHBOARD_URL
// env var is still used for the misconfigured-banner check + the
// fallback "open in new tab" link (when the proxy is unreachable).
$iframeSrc = ($misconfigured || $noPatient)
    ? ''
    : '/dashboard/patient/' . rawurlencode((string) $pid);

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
<?php } elseif ($noPatient) { ?>
    <div role="status" style="display:flex;align-items:center;justify-content:center;height:100%;padding:2rem;text-align:center;font-family:system-ui,sans-serif;color:#444;">
        <div>
            <div style="font-size:1.05rem;font-weight:600;margin-bottom:0.4rem;">No patient selected</div>
            <div style="font-size:0.9rem;color:#666;">Open a patient chart from the OpenEMR sidebar, then return to this tab.</div>
        </div>
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
<script>
    // Poll the OpenEMR session for the active patient pid. When it changes
    // (user picked a different patient via OpenEMR's chrome), reload this
    // tab so index.php re-renders with the new pid (or the empty state).
    // Document is hidden → skip polling to avoid background traffic.
    (function () {
        var renderedPid = <?php echo (int) $pid; ?>;
        var endpoint = 'current-pid.php';
        var intervalMs = 2500;
        var inFlight = false;

        function check() {
            if (inFlight || document.hidden) return;
            inFlight = true;
            fetch(endpoint, { credentials: 'same-origin', cache: 'no-store' })
                .then(function (r) { return r.ok ? r.json() : null; })
                .then(function (body) {
                    if (!body || typeof body.pid !== 'number') return;
                    if (body.pid !== renderedPid) {
                        // Pid changed (or cleared): full reload picks up
                        // both the iframe-vs-empty-state branch and the
                        // new patient context atomically.
                        window.location.reload();
                    }
                })
                .catch(function () { /* network blip; try again next tick */ })
                .finally(function () { inFlight = false; });
        }

        setInterval(check, intervalMs);
        // Also check immediately when the tab becomes visible again, so
        // returning from another OpenEMR tab feels instant.
        document.addEventListener('visibilitychange', function () {
            if (!document.hidden) check();
        });
    })();
</script>
</body>
</html>
