<?php

declare(strict_types=1);

namespace OpenEMR\Modules\ClaimDmd\CoPilot;

/**
 * BriefingController
 *
 * Handles requests for patient clinical briefings within the CoPilot feature.
 *
 * SECURITY NOTE (VUL-0001):
 *   Panel authorization is enforced unconditionally before any patient data
 *   is fetched or returned. This controller never returns clinical data for
 *   a patient who is not in the authenticated clinician's assigned panel,
 *   regardless of how the request was constructed or what the conversation
 *   history contains.
 */
class BriefingController
{
    private PanelAuthorizationMiddleware $panelAuth;
    private FhirPatientDataService $fhirService;

    public function __construct(
        PanelAuthorizationMiddleware $panelAuth,
        FhirPatientDataService $fhirService
    ) {
        $this->panelAuth   = $panelAuth;
        $this->fhirService = $fhirService;
    }

    /**
     * Fetch and return a clinical briefing for a patient.
     *
     * @param string $clinicianId  Authenticated clinician ID (from session — NOT from conversation).
     * @param string $patientId    Patient identifier to fetch.
     *
     * @return array Clinical briefing data.
     *
     * @throws PanelAuthorizationException  If the patient is not in the clinician's panel.
     *                                      Caller MUST NOT return any patient data in this case.
     */
    public function getBriefing(string $clinicianId, string $patientId): array
    {
        // ----------------------------------------------------------------
        // SECURITY GATE (VUL-0001): Panel authorization MUST be the first
        // operation. The FHIR fetch below MUST NOT be reached if this
        // throws. This is not optional and must not be conditional.
        // ----------------------------------------------------------------
        $this->panelAuth->assertPatientInPanel($clinicianId, $patientId);

        // Only reached if the patient is confirmed in-panel.
        return $this->fhirService->getPatientBriefing($patientId);
    }
}
