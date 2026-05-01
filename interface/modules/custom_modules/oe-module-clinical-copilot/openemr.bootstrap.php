<?php

/**
 * Clinical Co-Pilot — OpenEMR Custom Module Bootstrap
 *
 * Instantiates the module Bootstrap class and subscribes it to OpenEMR events.
 * Variables in scope here (provided by ModulesApplication): $module, $eventDispatcher.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot Contributors
 * @copyright Copyright (c) 2024 Clinical Co-Pilot Contributors
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

use OpenEMR\Modules\ClinicalCopilot\Bootstrap;

require_once __DIR__ . '/src/Bootstrap.php';

(new Bootstrap($eventDispatcher))->subscribeToEvents();
