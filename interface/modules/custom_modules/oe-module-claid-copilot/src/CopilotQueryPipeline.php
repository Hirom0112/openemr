<?php

/**
 * CopilotQueryPipeline
 *
 * Orchestrates the Clinical AI Copilot query execution path.
 * Security: Panel authorization is enforced as the FIRST synchronous blocking
 * step before any FHIR resource is queried. No clinical content is returned
 * unless authorization is confirmed. (VUL-0017)
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClaidCopilot;

use OpenEMR\Modules\ClaidCopilot\Exception\UnauthorizedPanelAccessException;
use OpenEMR\Modules\ClaidCopilot\Exception\LowConfidenceResolutionException;
use OpenEMR\Modules\ClaidCopilot\Exception\MisrouteHardStopException;

class CopilotQueryPipeline
{
    private PatientContextResolver $contextResolver;
    private PanelAuthorizationService $panelAuth;
    private FhirQueryGateway $fhirGateway;
    private CopilotResponseEnvelope $envelope;
    /** @var \OpenEMR\Common\Logging\SystemLogger */
    private $logger;

    public function __construct(
        PatientContextResolver $contextResolver,
        PanelAuthorizationService $panelAuth,
        FhirQueryGateway $fhirGateway,
        CopilotResponseEnvelope $envelope,
        $logger
    ) {
        $this->contextResolver = $contextResolver;
        $this->panelAuth       = $panelAuth;
        $this->fhirGateway     = $fhirGateway;
        $this->envelope        = $envelope;
        $this->logger          = $logger;
    }

    /**
     * Execute a Copilot query turn.
     *
     * @param string $rawQuery        Natural-language query from the user.
     * @param array  $sessionContext  Authenticated session context, including
     *                                session_patient_id (the EHR-bound patient)
     *                                and authenticated_user_id.
     * @return array                  Opaque response envelope (never leaks
     *                                patient_id / records_fetched on error).
     */
    public function execute(string $rawQuery, array $sessionContext): array
    {
        $authenticatedUserId    = $sessionContext['authenticated_user_id'] ?? null;
        $sessionBoundPatientId  = $sessionContext['session_patient_id'] ?? null;

        if (empty($authenticatedUserId)) {
            return $this->envelope->buildOpaqueError('UNAUTHENTICATED');
        }

        // ------------------------------------------------------------------ //
        // STEP 1 — Resolve patient context from query.                        //
        // The resolver treats prose-derived identifiers as UNTRUSTED and      //
        // enforces confidence thresholds + regex validation internally.       //
        // ------------------------------------------------------------------ //
        try {
            $resolvedContext = $this->contextResolver->resolve($rawQuery, $sessionContext);
        } catch (LowConfidenceResolutionException $e) {
            $this->logger->warning(
                '[VUL-0017] Low-confidence patient identifier resolution rejected',
                [
                    'user_id'    => $authenticatedUserId,
                    'confidence' => $e->getConfidence(),
                ]
            );
            return $this->envelope->buildOpaqueError('IDENTIFIER_RESOLUTION_REJECTED');
        }

        $resolvedPatientId = $resolvedContext['patient_id'] ?? null;

        // ------------------------------------------------------------------ //
        // STEP 2 — Session-bound patient context guard.                       //
        // The resolved patient_id MUST match the session-bound patient.       //
        // Cross-patient queries within a single session are blocked unless    //
        // the session has no bound patient (in which case we bind it now).    //
        // ------------------------------------------------------------------ //
        if (!empty($sessionBoundPatientId) && $resolvedPatientId !== $sessionBoundPatientId) {
            $this->logger->warning(
                '[VUL-0017] Cross-patient query blocked: resolved patient differs from session-bound patient',
                [
                    'user_id'             => $authenticatedUserId,
                    'session_patient_id'  => $sessionBoundPatientId,
                    // Do NOT log resolved_patient_id to avoid PHI in logs.
                ]
            );
            return $this->envelope->buildOpaqueError('CROSS_PATIENT_QUERY_DENIED');
        }

        // ------------------------------------------------------------------ //
        // STEP 3 — Misroute hard stop.                                        //
        // If the resolver detected a misroute and could not self-correct,     //
        // halt immediately — no clinical content is returned.                 //
        // ------------------------------------------------------------------ //
        if (
            ($resolvedContext['misroute_detected'] ?? false) === true &&
            ($resolvedContext['self_corrected']    ?? false) === false
        ) {
            $this->logger->alert(
                '[VUL-0017] Misroute detected and not self-corrected — hard stop',
                ['user_id' => $authenticatedUserId]
            );
            return $this->envelope->buildOpaqueError('MISROUTE_HARD_STOP');
        }

        // ------------------------------------------------------------------ //
        // STEP 4 — Panel authorization gate (SYNCHRONOUS, BLOCKING).         //
        // This MUST succeed before any FHIR call is issued.                  //
        // ------------------------------------------------------------------ //
        try {
            $authToken = $this->panelAuth->assertAuthorized($authenticatedUserId, $resolvedPatientId);
        } catch (UnauthorizedPanelAccessException $e) {
            // Audit log written inside assertAuthorized(); no PHI in this log.
            $this->logger->warning(
                '[VUL-0017] Panel authorization denied — FHIR query suppressed',
                ['user_id' => $authenticatedUserId]
            );
            return $this->envelope->buildOpaqueError('PANEL_AUTHORIZATION_DENIED');
        }

        // ------------------------------------------------------------------ //
        // STEP 5 — Execute FHIR query (only reachable after authorization).   //
        // ------------------------------------------------------------------ //
        try {
            $fhirResult = $this->fhirGateway->query($resolvedContext, $authToken);
        } catch (\Throwable $e) {
            $this->logger->error(
                '[CopilotQueryPipeline] FHIR query failed',
                ['user_id' => $authenticatedUserId, 'error' => $e->getMessage()]
            );
            return $this->envelope->buildOpaqueError('FHIR_QUERY_FAILED');
        }

        // ------------------------------------------------------------------ //
        // STEP 6 — Build and sanitize response envelope.                      //
        // The envelope applies output-side canary filtering.                  //
        // ------------------------------------------------------------------ //
        return $this->envelope->buildSuccess($fhirResult, $sessionContext);
    }
}
