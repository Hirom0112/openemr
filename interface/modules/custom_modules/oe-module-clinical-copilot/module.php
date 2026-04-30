<?php

/**
 * Clinical Co-Pilot — OpenEMR Custom Module registration.
 *
 * Registers the module with OpenEMR's module registry.
 * This file contains no business logic.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot Contributors
 * @copyright Copyright (c) 2024 Clinical Co-Pilot Contributors
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

use OpenEMR\Core\ModulesClassLoader;

$loader = new ModulesClassLoader($GLOBALS['fileroot']);

return [
    'id'          => 'oe-module-clinical-copilot',
    'name'        => 'Clinical Co-Pilot',
    'description' => 'AI-assisted clinical decision support sidebar for rounding hospitalists.',
    'version'     => '0.1.0',
    'author'      => 'Clinical Co-Pilot Contributors',
    'license'     => 'GPL-3.0',
    'acl'         => [],
];
