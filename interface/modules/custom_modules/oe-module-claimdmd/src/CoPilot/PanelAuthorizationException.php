<?php

declare(strict_types=1);

namespace OpenEMR\Modules\ClaimDmd\CoPilot;

/**
 * Thrown when a CoPilot tool call requests data for a patient outside the
 * authenticated clinician's assigned panel.
 *
 * Callers MUST catch this exception and return an appropriate error response
 * without leaking any patient data.
 */
class PanelAuthorizationException extends \RuntimeException
{
}
