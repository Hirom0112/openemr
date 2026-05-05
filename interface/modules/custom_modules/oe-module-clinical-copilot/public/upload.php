<?php

/**
 * Clinical Co-Pilot — Document upload public entry point.
 *
 * Thin shim that boots OpenEMR's globals (so ``sqlQuery`` + the legacy
 * ``Document`` class are available), declares this as an unauthenticated
 * (session-wise) API endpoint — the controller verifies its own HS256 JWT —
 * and dispatches to UploadController::handle().
 *
 * Public URL once deployed:
 *   /interface/modules/custom_modules/oe-module-clinical-copilot/public/upload.php
 *
 * The agent-api POSTs multipart here as a third-tier write fallback after the
 * FHIR Binary path and the legacy /apis/default/api/patient/{pid}/document
 * path both fail.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot Contributors
 * @copyright Copyright (c) 2026 Clinical Co-Pilot Contributors
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

// Tell globals.php this is a no-session API endpoint. The controller does
// its own JWT-based auth, so we deliberately bypass OpenEMR's session +
// CSRF gates (the agent-api is a server-to-server caller).
$ignoreAuth        = true;
$sessionAllowWrite = false;
$skipFinishLogin   = true;

require_once __DIR__ . '/../../../../globals.php';
require_once __DIR__ . '/../src/UploadController.php';
require_once __DIR__ . '/../../../../../library/classes/Document.class.php';

use OpenEMR\Modules\ClinicalCopilot\UploadController;

UploadController::handle();
