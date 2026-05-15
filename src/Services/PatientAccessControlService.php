<?php

/**
 * PatientAccessControlService
 *
 * Enforces panel-membership access control for clinical patient data retrieval.
 * All code paths that retrieve patient records — whether from structured queries
 * or natural-language AI copilot input — MUST call assertPatientInPanel() or
 * filterAuthorizedPatients() before returning any PHI to the caller.
 *
 * Addresses: VUL-0008 (Cross-patient data leakage via missing panel enforcement)
 * OWASP LLM: LLM02:2025
 * MITRE ATLAS: AML.T0057
 * HIPAA: 164.312(a)(1), 164.308(a)(4)
 *
 * @package   OpenEMR
 * @subpackage Services
 */

declare(strict_types=1);

namespace OpenEMR\Services;

use OpenEMR\Common\Logging\SystemLogger;
use OpenEMR\Common\Auth\OpenIDConnect\Repositories\AccessTokenRepository;
use OpenEMR\Services\PatientAccessDeniedException;

class PatientAccessControlService
{
    /** @var SystemLogger */
    private SystemLogger $logger;

    /** @var string|null Authenticated clinician user UUID derived from session */
    private ?string $userUuid;

    /** @var array<string>|null Cached panel patient IDs for the current session */
    private ?array $panelCache = null;

    /**
     * @param string|null $userUuid  The authenticated user's UUID (from session).
     *                               Pass null only for non-clinician sessions that
     *                               rely solely on legacy ACL and have no panel.
     */
    public function __construct(?string $userUuid = null)
    {
        $this->logger   = new SystemLogger();
        $this->userUuid = $userUuid ?? $this->resolveUserUuidFromSession();
    }

    // -------------------------------------------------------------------------
    // Public API
    // -------------------------------------------------------------------------

    /**
     * Assert that $patientId is within the authenticated clinician's panel.
     *
     * @param  string $patientId  The patient identifier to validate (any format).
     * @throws PatientAccessDeniedException  If the patient is not in the panel.
     *                                       The exception message MUST NOT reveal
     *                                       whether the patient exists.
     */
    public function assertPatientInPanel(string $patientId): void
    {
        // If we have no user context (e.g. admin/system session with full ACL),
        // defer to legacy access controls and skip panel check.
        if ($this->userUuid === null) {
            return;
        }

        $panel = $this->getPanelForSession();

        // A null panel means this user has no panel configured; deny everything
        // so that an unconfigured state fails closed, not open.
        if ($panel === null || !in_array($patientId, $panel, true)) {
            $this->logUnauthorizedAttempt($patientId);
            throw new PatientAccessDeniedException(
                'Access to the requested patient record is not authorized for the current session.'
            );
        }
    }

    /**
     * Filter an array of patient IDs to only those within the authenticated
     * clinician's panel.  Out-of-panel IDs are silently removed (not redacted)
     * and each removal is individually audit-logged.
     *
     * Use this for bulk/handoff/sign-out responses where partial results are
     * acceptable rather than an all-or-nothing refusal.
     *
     * @param  array<string> $patientIds
     * @return array<string>  Subset of $patientIds that are panel-authorized.
     */
    public function filterAuthorizedPatients(array $patientIds): array
    {
        if ($this->userUuid === null) {
            // No user context — return all; legacy ACL governs.
            return $patientIds;
        }

        $panel      = $this->getPanelForSession();
        $authorized = [];

        foreach ($patientIds as $pid) {
            if ($panel !== null && in_array($pid, $panel, true)) {
                $authorized[] = $pid;
            } else {
                $this->logUnauthorizedAttempt($pid);
            }
        }

        return $authorized;
    }

    /**
     * Return the full authorized panel for the current session, or null if
     * the user has no panel (treated as zero-record panel).
     *
     * @return array<string>|null
     */
    public function getAuthorizedPanel(): ?array
    {
        return $this->getPanelForSession();
    }

    // -------------------------------------------------------------------------
    // Private helpers
    // -------------------------------------------------------------------------

    /**
     * Resolve the authenticated user's UUID from the PHP session.
     * Returns null if the session does not contain a user UUID (e.g. CLI context).
     */
    private function resolveUserUuidFromSession(): ?string
    {
        if (session_status() !== PHP_SESSION_ACTIVE) {
            return null;
        }
        return $_SESSION['authUserID'] ?? null;
    }

    /**
     * Fetch and cache the panel (list of patient IDs) assigned to the
     * authenticated clinician.
     *
     * The panel is determined by the `patient_access_panel` table, keyed on
     * the clinician's user UUID.  The result is cached per-request (panelCache)
     * to avoid repeated DB hits; it is NEVER populated from user-supplied input.
     *
     * @return array<string>|null  Null means the user exists but has no panel rows.
     */
    private function getPanelForSession(): ?array
    {
        if ($this->panelCache !== null) {
            return $this->panelCache;
        }

        if ($this->userUuid === null) {
            return null;
        }

        // Query the panel table.  The table schema expected:
        //   CREATE TABLE patient_access_panel (
        //     id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        //     user_uuid   VARCHAR(64) NOT NULL,
        //     patient_id  VARCHAR(64) NOT NULL,
        //     UNIQUE KEY uq_user_patient (user_uuid, patient_id)
        //   );
        $sql  = 'SELECT patient_id FROM patient_access_panel WHERE user_uuid = ?';
        $rows = sqlStatementNoLog($sql, [$this->userUuid]);

        if ($rows === false) {
            // DB error — fail closed.
            $this->logger->error(
                '[PatientAccessControlService] DB error fetching panel',
                ['user_uuid' => $this->userUuid]
            );
            return [];
        }

        $panel = [];
        while ($row = sqlFetchArray($rows)) {
            $panel[] = (string) $row['patient_id'];
        }

        $this->panelCache = $panel;
        return $this->panelCache;
    }

    /**
     * Emit a HIPAA 164.308(a)(4) audit log entry for an unauthorized access
     * attempt.  The log entry MUST NOT include any PHI about the patient.
     *
     * @param string $patientId  The patient ID that was refused.
     */
    private function logUnauthorizedAttempt(string $patientId): void
    {
        // Mask the patient ID in the log to prevent PHI leakage into log files;
        // we record only a truncated hash for correlation without exposure.
        $maskedId = 'pid:' . substr(hash('sha256', $patientId), 0, 12);

        $this->logger->warning(
            '[HIPAA-AUDIT][VUL-0008] Unauthorized out-of-panel patient access attempt',
            [
                'user_uuid'     => $this->userUuid,
                'masked_pid'    => $maskedId,
                'session_id'    => session_id(),
                'remote_addr'   => $_SERVER['REMOTE_ADDR'] ?? 'unknown',
                'request_uri'   => $_SERVER['REQUEST_URI'] ?? 'unknown',
                'timestamp'     => gmdate('c'),
            ]
        );

        // Also write to the OpenEMR event log for SOC visibility.
        newEvent(
            'patient-access-denied',
            $GLOBALS['authUser'] ?? 'unknown',
            $GLOBALS['authProvider'] ?? 'unknown',
            0,
            'Out-of-panel patient access attempt blocked (VUL-0008). Masked PID: ' . $maskedId
        );
    }
}
