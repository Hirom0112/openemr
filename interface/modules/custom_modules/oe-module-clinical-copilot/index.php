<?php

/**
 * Clinical Co-Pilot — sidebar shell page.
 *
 * Authenticates the OpenEMR session, extracts provider context, and
 * returns an HTML page that loads the compiled React bundle.
 *
 * This file does no request handling, no proxying, and no state management.
 * Every rounding query travels directly from the React panel to agent-api.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot Contributors
 * @copyright Copyright (c) 2024 Clinical Co-Pilot Contributors
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

require_once __DIR__ . '/../../../../globals.php';

use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Csrf\CsrfUtils;

// Require a valid OpenEMR session with at least patient-data read access.
if (!AclMain::aclCheckCore('patients', 'med')) {
    http_response_code(403);
    echo 'Access denied.';
    exit;
}

$agentApiUrl  = $GLOBALS['copilot_agent_api_url'] ?? 'http://localhost:8400';
$csrfToken    = CsrfUtils::collectCsrfToken();
$providerId   = (string) ($_SESSION['authUserID'] ?? '');
$providerName = (string) ($_SESSION['authUser'] ?? 'Provider');
// Tie the rounding session to the OpenEMR PHP session so the agent checkpointer
// and the browser share the same session key across page reloads.
$sessionId    = session_id() ?: uniqid('copilot-', true);
// Patient IDs are pre-populated by the census workflow that launches this panel.
// Empty array is safe — the React panel auto-dispatches a census query on mount
// which loads the full list from the agent-api checkpointer.
$patientIds   = $_SESSION['copilot_patient_ids'] ?? [];

?>
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Clinical Co-Pilot</title>
</head>
<body>
    <div id="copilot-root"></div>

    <script>
        window.__COPILOT_CONFIG__ = {
            agentApiUrl:  <?php echo json_encode($agentApiUrl, JSON_THROW_ON_ERROR); ?>,
            providerId:   <?php echo json_encode($providerId, JSON_THROW_ON_ERROR); ?>,
            providerName: <?php echo json_encode($providerName, JSON_THROW_ON_ERROR); ?>,
            csrfToken:    <?php echo json_encode($csrfToken, JSON_THROW_ON_ERROR); ?>,
            sessionId:    <?php echo json_encode($sessionId, JSON_THROW_ON_ERROR); ?>,
            patientIds:   <?php echo json_encode(array_values($patientIds), JSON_THROW_ON_ERROR); ?>
        };
    </script>
    <script src="public/copilot.js"></script>
</body>
</html>
