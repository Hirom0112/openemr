<?php

/**
 * PatientContextResolver
 *
 * Resolves patient context from a Copilot query turn.
 *
 * Security hardening (VUL-0017):
 * - All patient-identifying information derived from user-supplied prose is
 *   treated as UNTRUSTED DATA, not as authoritative routing instructions.
 * - Identifier resolution from prose descriptions (e.g. "begins with 0-1-8")
 *   is subjected to regex validation and a minimum confidence threshold.
 * - Resolutions below the threshold throw LowConfidenceResolutionException
 *   rather than falling back to the LLM.
 * - The resolved patient_id is NEVER included in error/refusal return values.
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClaidCopilot;

use OpenEMR\Modules\ClaidCopilot\Exception\LowConfidenceResolutionException;

class PatientContextResolver
{
    /**
     * Minimum confidence required to promote a prose-inferred identifier
     * to canonical patient_id form. Values below this threshold are rejected.
     */
    private const MINIMUM_CONFIDENCE_THRESHOLD = 0.85;

    /**
     * Canonical patient identifier format.  Only strings matching this pattern
     * are eligible for use as a FHIR query patient_id.
     */
    private const PATIENT_ID_REGEX = '/^pt-\d+$/i';

    /** @var mixed LLM/NLP backend for query understanding */
    private $nlpBackend;

    public function __construct($nlpBackend)
    {
        $this->nlpBackend = $nlpBackend;
    }

    /**
     * Resolve patient context from a raw query and session context.
     *
     * Returns an array with keys:
     *   patient_id        (string)  — canonical patient identifier
     *   resource_type     (string)  — FHIR resource type to query
     *   misroute_detected (bool)
     *   self_corrected    (bool)
     *   source            (string)  — 'session' | 'prose_inference'
     *
     * @throws LowConfidenceResolutionException if confidence < threshold
     * @throws \InvalidArgumentException         if identifier fails regex
     */
    public function resolve(string $rawQuery, array $sessionContext): array
    {
        // Prefer the EHR-injected, system-verified session patient binding.
        $sessionPatientId = $sessionContext['session_patient_id'] ?? null;

        if (!empty($sessionPatientId)) {
            // System-verified identifier: validate format, then trust.
            $this->assertIdentifierFormat($sessionPatientId);
            return [
                'patient_id'        => $sessionPatientId,
                'resource_type'     => $this->resolveResourceType($rawQuery),
                'misroute_detected' => false,
                'self_corrected'    => false,
                'source'            => 'session',
            ];
        }

        // No session-bound patient: attempt NLP extraction from prose.
        // This path is treated as HIGH-RISK and is confidence-gated.
        $nlpResult = $this->nlpBackend->extractPatientContext($rawQuery);

        $inferredId  = $nlpResult['patient_id']  ?? null;
        $confidence  = (float)($nlpResult['confidence'] ?? 0.0);
        $misroute    = (bool)($nlpResult['misroute_detected'] ?? false);
        $selfCorrected = (bool)($nlpResult['self_corrected'] ?? false);

        // Gate 1: confidence threshold
        if ($confidence < self::MINIMUM_CONFIDENCE_THRESHOLD) {
            throw new LowConfidenceResolutionException(
                sprintf(
                    'Prose-derived patient identifier confidence %.2f is below minimum threshold %.2f — resolution rejected.',
                    $confidence,
                    self::MINIMUM_CONFIDENCE_THRESHOLD
                ),
                $confidence
            );
        }

        // Gate 2: identifier format validation
        if (!empty($inferredId)) {
            $this->assertIdentifierFormat($inferredId);
        }

        return [
            'patient_id'        => $inferredId,
            'resource_type'     => $this->resolveResourceType($rawQuery),
            'misroute_detected' => $misroute,
            'self_corrected'    => $selfCorrected,
            'source'            => 'prose_inference',
        ];
    }

    /**
     * Assert that a patient identifier matches the canonical format.
     *
     * @throws \InvalidArgumentException
     */
    private function assertIdentifierFormat(string $patientId): void
    {
        if (!preg_match(self::PATIENT_ID_REGEX, $patientId)) {
            // Do NOT include the offending value in the exception message
            // to avoid propagating potentially adversarial input.
            throw new \InvalidArgumentException(
                'Patient identifier does not match required canonical format and has been rejected.'
            );
        }
    }

    /**
     * Determine the FHIR resource type from the query intent.
     * This is safe to derive from user input as it does not gate authorization.
     */
    private function resolveResourceType(string $rawQuery): string
    {
        // Simplified intent mapping — extend as needed.
        $lower = strtolower($rawQuery);
        if (str_contains($lower, 'observation') || str_contains($lower, 'vital') || str_contains($lower, 'ecg') || str_contains($lower, 'electrocardiogram')) {
            return 'Observation';
        }
        if (str_contains($lower, 'condition') || str_contains($lower, 'diagnosis') || str_contains($lower, 'diagnostic')) {
            return 'Condition';
        }
        if (str_contains($lower, 'medication')) {
            return 'MedicationRequest';
        }
        return 'Observation'; // safe default
    }
}
