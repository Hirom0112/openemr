<?php

/**
 * Clinical Co-Pilot — tab page.
 *
 * Loaded by OpenEMR in the content iframe when the user clicks the
 * "Co-Pilot" navigation tab. Authenticates the session, builds config,
 * and renders the React bundle mount point.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot Contributors
 * @copyright Copyright (c) 2024 Clinical Co-Pilot Contributors
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

// OpenEMR stores auth keys at the top level of $_SESSION on the published
// Docker image, but the local dev branch nests them under $_SESSION['OpenEMR']
// after the HttpSessionFactory refactor. Read from both so the module works
// against either layout.
$oemrSession  = $_SESSION['OpenEMR'] ?? [];
$agentApiUrl  = getenv('COPILOT_AGENT_API_URL') ?: ($GLOBALS['copilot_agent_api_url'] ?? 'http://localhost:8400');
$providerId   = (int) ($oemrSession['authUserID'] ?? $_SESSION['authUserID'] ?? 0);
$providerName = (string) ($oemrSession['authUser'] ?? $_SESSION['authUser'] ?? '');
$sessionId    = session_id() ?: uniqid('copilot-', true);
$patientIds   = $oemrSession['copilot_patient_ids'] ?? $_SESSION['copilot_patient_ids'] ?? [];
if (empty($patientIds) && !empty($_GET['pids'])) {
    $patientIds = array_filter(array_map('intval', explode(',', $_GET['pids'])));
}

// Auto-populate from open encounters assigned to this provider in the last 7 days.
// date_end IS NULL means the encounter has not been closed/discharged yet.
// Falls back to this only when neither the session variable nor ?pids= is set.
if (empty($patientIds) && $providerId > 0) {
    $encounterResult = sqlStatement(
        "SELECT DISTINCT pid
           FROM form_encounter
          WHERE provider_id = ?
            AND (date_end IS NULL OR date_end = '0000-00-00 00:00:00')
            AND date >= DATE_SUB(NOW(), INTERVAL 7 DAY)
          ORDER BY date ASC",
        [$providerId]
    );
    while ($row = sqlFetchArray($encounterResult)) {
        $patientIds[] = (int) $row['pid'];
    }
}

$config = [
    'agentApiUrl'  => $agentApiUrl,
    'providerId'   => $providerId,
    'providerName' => $providerName,
    'sessionId'    => $sessionId,
    // Cast to strings — the agent API's Pydantic schema validates patient_ids
    // as list[str], and coerce_numbers_to_str doesn't reach nested list items.
    'patientIds'   => array_map('strval', array_values($patientIds)),
];

$configJson = json_encode($config, JSON_HEX_TAG | JSON_HEX_APOS | JSON_HEX_QUOT | JSON_HEX_AMP | JSON_THROW_ON_ERROR);

?>
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Clinical Co-Pilot</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        html, body { height: 100%; overflow: hidden; }
    </style>
</head>
<body>
    <span class="title" style="display:none;">Clinical Co-Pilot</span>
    <div id="copilot-root"></div>
    <script type="application/json" id="copilot-config"><?php echo $configJson; ?></script>
    <script>
    // Force the parent OpenEMR tab title to "Clinical Co-Pilot". The default
    // Knockout binding sniffs the iframe document and falls back to "Unknown"
    // when its heuristics miss; setting tabsList[i].title() directly bypasses
    // that path entirely.
    (function () {
        function setTabTitle() {
            try {
                var topWin = window.parent;
                if (!topWin || topWin === window || !topWin.app_view_model) { return; }
                var tabs = topWin.app_view_model.application_data
                    && topWin.app_view_model.application_data.tabs
                    && topWin.app_view_model.application_data.tabs.tabsList;
                if (typeof tabs !== 'function') { return; }
                var list = tabs();
                var hit = 0;
                for (var i = 0; i < list.length; i++) {
                    var t = list[i];
                    var name = (t && typeof t.name === 'function') ? t.name() : null;
                    var url  = (t && typeof t.url  === 'function') ? t.url()  : '';
                    var matches = (name === 'cop')
                        || (typeof url === 'string' && url.indexOf('oe-module-clinical-copilot') !== -1);
                    if (matches && typeof t.title === 'function') {
                        t.title('Clinical Co-Pilot');
                        hit++;
                    }
                }
                console.log('[Co-Pilot] setTabTitle ran, matched ' + hit + ' tab(s)');
            } catch (e) {
                console.warn('[Co-Pilot] setTabTitle error:', e);
            }
        }
        // Run now and again after the parent's iframe-load handler fires,
        // which would otherwise sniff the iframe and may overwrite the title.
        setTabTitle();
        setTimeout(setTabTitle, 0);
        setTimeout(setTabTitle, 250);
        setTimeout(setTabTitle, 1000);
    })();
    </script>
    <script src="public/copilot.js?v=<?php echo filemtime(__DIR__ . '/public/copilot.js'); ?>"></script>
</body>
</html>
