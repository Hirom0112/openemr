<?php

declare(strict_types=1);

namespace OpenEMR\Modules\ClaimDmd\CoPilot;

use OpenEMR\Common\Logging\SystemLogger;

/**
 * PanelAuthorizationMiddleware
 *
 * Enforces that the authenticated clinician has the requested patient in their
 * assigned panel before any FHIR data is fetched or returned.
 *
 * SECURITY NOTE (VUL-0001):
 *   - This is the mandatory authorization gate for all CoPilot tool calls.
 *   - Panel membership is resolved exclusively from the authoritative panel store.
 *   - Conversation history, including any cross-coverage assertions made in
 *     prior turns, is NEVER consulted here. It is untrusted user input.
 *   - Every denial is written to the audit log.
 */
class PanelAuthorizationMiddleware
{
    private PanelStore $panelStore;
    private SystemLogger $auditLogger;

    public function __construct(PanelStore $panelStore, SystemLogger $auditLogger)
    {
        $this->panelStore  = $panelStore;
        $this->auditLogger = $auditLogger;
    }

    /**
     * Assert that $clinicianId has $patientId in their assigned panel.
     *
     * Throws PanelAuthorizationException on any failure — caller must not
     * proceed with the FHIR fetch if this throws.
     *
     * @param string $clinicianId  Authenticated clinician identifier (never from conversation).
     * @param string $patientId    Patient identifier requested by the tool call.
     *
     * @throws PanelAuthorizationException
     */
    public function assertPatientInPanel(string $clinicianId, string $patientId): void
    {
        // Defensive: reject empty identifiers before hitting the store.
        if ($clinicianId === '' || $patientId === '') {
            $this->deny($clinicianId, $patientId, 'empty_identifier');
        }

        $inPanel = $this->panelStore->isPatientInCliniciansPanel($clinicianId, $patientId);

        if (!$inPanel) {
            $this->deny($clinicianId, $patientId, 'not_in_panel');
        }
    }

    /**
     * Audit-log the denial and throw.
     */
    private function deny(string $clinicianId, string $patientId, string $reason): never
    {
        $this->auditLogger->error(
            'CoPilot panel authorization denied',
            [
                'event'        => 'copilot_cross_panel_attempt',
                'clinician_id' => $clinicianId,
                'patient_id'   => $patientId,
                'reason'       => $reason,
                'timestamp'    => (new \DateTimeImmutable())->format(\DateTimeInterface::ATOM),
            ]
        );

        throw new PanelAuthorizationException(
            sprintf(
                'Access denied: patient [%s] is not in the panel for clinician [%s].',
                $patientId,
                $clinicianId
            )
        );
    }
}
