<?php

/**
 * CopilotResponseEnvelope
 *
 * Builds and sanitizes response envelopes for the Clinical AI Copilot.
 *
 * Security hardening (VUL-0017):
 * - Output-side canary filter: patient_id, session_id, and records_fetched are
 *   suppressed from any envelope where patient_id is not in the session's
 *   authorized patient list.
 * - Error/refusal envelopes are always opaque — they never echo back resolved
 *   patient_id, session_id, or records_fetched count.
 * - Success envelopes are filtered through the canary gate before return.
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClaidCopilot;

class CopilotResponseEnvelope
{
    /**
     * Build an opaque error response.
     *
     * No PHI fields (patient_id, session_id, records_fetched) are included.
     *
     * @param string $errorCode  A machine-readable error code (no PHI).
     * @return array
     */
    public function buildOpaqueError(string $errorCode): array
    {
        return [
            'status'  => 'error',
            'code'    => $errorCode,
            'message' => 'The request could not be completed. Please contact your system administrator if this problem persists.',
            // Deliberately omitted: patient_id, session_id, records_fetched
        ];
    }

    /**
     * Build a successful response envelope, applying the output-side canary filter.
     *
     * @param array $fhirResult      Raw FHIR query result.
     * @param array $sessionContext  Authenticated session context.
     * @return array
     */
    public function buildSuccess(array $fhirResult, array $sessionContext): array
    {
        $authorizedPatientId = $sessionContext['session_patient_id'] ?? null;
        $resultPatientId     = $fhirResult['patient_id'] ?? null;

        // Output-side canary filter:
        // If the patient_id in the FHIR result is not the session-authorized
        // patient, suppress all PHI fields and return an opaque error instead.
        // This is a defense-in-depth layer covering cases where the upstream
        // panel gate was somehow bypassed.
        if (
            !empty($authorizedPatientId) &&
            !empty($resultPatientId) &&
            $resultPatientId !== $authorizedPatientId
        ) {
            return $this->buildOpaqueError('OUTPUT_CANARY_TRIGGERED');
        }

        // Build the sanitized success envelope.
        // Strip fields that could constitute information disclosure if the
        // response is intercepted or logged.
        $safeEntry = $this->stripSensitiveMetadata($fhirResult, $sessionContext);

        return [
            'status'  => 'success',
            'payload' => $safeEntry,
        ];
    }

    /**
     * Remove sensitive metadata fields from a result array before returning
     * to the caller.
     *
     * Fields removed: session_id (echoed from request), records_fetched
     * (information disclosure), internal routing metadata.
     *
     * @param array $fhirResult
     * @param array $sessionContext
     * @return array
     */
    private function stripSensitiveMetadata(array $fhirResult, array $sessionContext): array
    {
        // Allow-list approach: only pass through fields that are safe to expose.
        $allowed = [
            'resourceType',
            'entry',
            'total',
            'link',
        ];

        $safe = [];
        foreach ($allowed as $key) {
            if (array_key_exists($key, $fhirResult)) {
                $safe[$key] = $fhirResult[$key];
            }
        }

        // Explicitly omit: patient_id, session_id, records_fetched,
        // misroute_detected, confidence, source, self_corrected.
        return $safe;
    }
}
