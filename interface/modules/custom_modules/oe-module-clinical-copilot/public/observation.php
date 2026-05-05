<?php

/**
 * Clinical Co-Pilot — FHIR Observation upload public entry point.
 *
 * Thin shim that boots OpenEMR's globals (so ``sqlQuery`` / ``sqlStatement``
 * are available) and dispatches to ObservationController::handle(). The
 * controller verifies its own HS256 JWT — server-to-server auth from the
 * agent-api — so we deliberately bypass OpenEMR's session + CSRF gates.
 *
 * Public URL once deployed:
 *   /interface/modules/custom_modules/oe-module-clinical-copilot/public/observation.php
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot Contributors
 * @copyright Copyright (c) 2026 Clinical Co-Pilot Contributors
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

$ignoreAuth        = true;
$sessionAllowWrite = false;
$skipFinishLogin   = true;

require_once __DIR__ . '/../../../../globals.php';
require_once __DIR__ . '/../src/ObservationController.php';

use OpenEMR\Modules\ClinicalCopilot\ObservationController;

ObservationController::handle();
