<?php

/**
 * Returns the current OpenEMR session's active patient pid as JSON.
 *
 * Polled by index.php's iframe shell every few seconds so the embedded
 * Next.js dashboard can detect when the user picks a different patient
 * via OpenEMR's chrome and reload accordingly. Read-only; no side
 * effects on the session.
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

header('Content-Type: application/json');
header('Cache-Control: no-store');

if (!AclMain::aclCheckCore('patients', 'med')) {
    http_response_code(403);
    echo json_encode(['error' => 'forbidden']);
    exit;
}

$oemrSession = $_SESSION['OpenEMR'] ?? [];
$rawPid = $oemrSession['pid'] ?? $_SESSION['pid'] ?? null;
$pid = (is_numeric($rawPid) && (int) $rawPid > 0) ? (int) $rawPid : 0;

echo json_encode(['pid' => $pid], JSON_THROW_ON_ERROR);
