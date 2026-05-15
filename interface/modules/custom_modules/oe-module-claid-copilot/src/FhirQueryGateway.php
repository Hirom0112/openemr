<?php

/**
 * FhirQueryGateway
 *
 * Executes FHIR resource queries on behalf of the Copilot.
 *
 * Security hardening (VUL-0017):
 * - Refuses to issue any FHIR request unless a valid PanelAuthorizationToken
 *   is supplied that covers the (user_id, patient_id) pair in the context.
 * - Returns an opaque error (no patient_id, no records_fetched) on failure.
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClaidCopilot;

use OpenEMR\Modules\ClaidCopilot\Exception\UnauthorizedPanelAccessException;
use OpenEMR\Modules\ClaidCopilot\PanelAuthorizationToken;

class FhirQueryGateway
{
    /** @var mixed FHIR client (e.g. HL7\FHIR\Client or equivalent) */
    private $fhirClient;

    public function __construct($fhirClient)
    {
        $this->fhirClient = $fhirClient;
    }

    /**
     * Execute a FHIR query.
     *
     * @param array                  $resolvedContext  Output of PatientContextResolver::resolve()
     * @param PanelAuthorizationToken $authToken       Token issued by PanelAuthorizationService::assertAuthorized()
     * @return array                                   Raw FHIR result set
     *
     * @throws UnauthorizedPanelAccessException if the token does not cover the requested patient
     * @throws \RuntimeException                on FHIR client errors
     */
    public function query(array $resolvedContext, PanelAuthorizationToken $authToken): array
    {
        $patientId    = $resolvedContext['patient_id'] ?? null;
        $resourceType = $resolvedContext['resource_type'] ?? 'Observation';

        // Hard gate: verify the authorization token covers this specific patient.
        if (!$authToken->coversPatient((string)$patientId)) {
            // This should never be reached if CopilotQueryPipeline is used
            // correctly, but serves as a defense-in-depth check.
            throw new UnauthorizedPanelAccessException(
                'FhirQueryGateway: authorization token does not cover the requested patient — query suppressed.'
            );
        }

        if (empty($patientId)) {
            throw new \InvalidArgumentException('FhirQueryGateway: patient_id must not be empty.');
        }

        // Execute the FHIR query only after authorization is confirmed.
        $result = $this->fhirClient->search($resourceType, ['patient' => $patientId]);

        return $result;
    }
}
