<?php

/**
 * Patient Dashboard Port — OpenEMR Custom Module registration.
 *
 * Registers the module with OpenEMR's module registry. Contains no
 * business logic.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Patient Dashboard Port Contributors
 * @copyright Copyright (c) 2026 Patient Dashboard Port Contributors
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

return [
    'id'          => 'oe-module-patient-dashboard-port',
    'name'        => 'Patient Dashboard (Port)',
    'description' => 'Modernized read-only patient dashboard rendered as a Next.js iframe.',
    'version'     => '0.1.0',
    'author'      => 'Patient Dashboard Port Contributors',
    'license'     => 'GPL-3.0',
    'acl'         => [],
];
