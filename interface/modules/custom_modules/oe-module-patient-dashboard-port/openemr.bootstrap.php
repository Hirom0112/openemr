<?php

/**
 * Patient Dashboard Port — OpenEMR Custom Module Bootstrap
 *
 * Instantiates the module Bootstrap class and subscribes it to OpenEMR
 * events. Variables in scope here (provided by ModulesApplication):
 * $module, $eventDispatcher.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Patient Dashboard Port Contributors
 * @copyright Copyright (c) 2026 Patient Dashboard Port Contributors
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

use OpenEMR\Modules\PatientDashboardPort\Bootstrap;

require_once __DIR__ . '/src/Bootstrap.php';

(new Bootstrap($eventDispatcher))->subscribeToEvents();
