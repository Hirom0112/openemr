<?php

/**
 * PatientAccessDeniedException
 *
 * Thrown when a clinician attempts to access a patient record that is not
 * within their authorized panel.  The exception message MUST NOT confirm
 * whether the requested patient exists (prevents enumeration attacks).
 *
 * Addresses: VUL-0008
 * OWASP LLM: LLM02:2025
 * MITRE ATLAS: AML.T0057
 * HIPAA: 164.312(a)(1)
 *
 * @package   OpenEMR
 * @subpackage Services
 */

declare(strict_types=1);

namespace OpenEMR\Services;

class PatientAccessDeniedException extends \RuntimeException
{
    public function __construct(
        string $message = 'Access to the requested patient record is not authorized for the current session.',
        int $code = 403,
        ?\Throwable $previous = null
    ) {
        parent::__construct($message, $code, $previous);
    }
}
