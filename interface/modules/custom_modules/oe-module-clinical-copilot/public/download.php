<?php

/**
 * Clinical Co-Pilot — Document download public entry point.
 *
 * Thin shim that boots OpenEMR's globals (so ``sqlQuery`` + the legacy
 * ``Document`` class are available), declares this as an unauthenticated
 * (session-wise) API endpoint — the controller verifies its own HS256 JWT —
 * and dispatches to DownloadController::handle().
 *
 * Public URL once deployed:
 *   /interface/modules/custom_modules/oe-module-clinical-copilot/public/download.php?id=NNN
 *
 * The agent-api fetches this on behalf of the agent-ui review panel so the
 * left rail can render the original PDF/PNG/JPEG/DOCX bytes alongside the
 * extracted facts. Mirrors upload.php for boot/auth shape.
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
require_once __DIR__ . '/../src/DownloadController.php';
require_once __DIR__ . '/../../../../../library/classes/Document.class.php';

use OpenEMR\Modules\ClinicalCopilot\DownloadController;

DownloadController::handle();
