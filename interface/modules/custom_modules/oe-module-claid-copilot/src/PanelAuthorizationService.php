<?php

/**
 * PanelAuthorizationService
 *
 * Verifies that a clinician has an established care-team or panel relationship
 * with a patient before any clinical data is retrieved.
 *
 * Security hardening (VUL-0017):
 * - assertAuthorized() throws UnauthorizedPanelAccessException (and writes an
 *   audit log entry) if the user has no panel relationship with the patient.
 * - isPanelMember() is a synchronous read used for pre-flight checks.
 * - Neither method accepts user-supplied context as a substitute for a
 *   database-verified relationship.
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClaidCopilot;

use OpenEMR\Modules\ClaidCopilot\Exception\UnauthorizedPanelAccessException;

class PanelAuthorizationService
{
    /** @var \PDO|\OpenEMR\Common\Database\QueryUtils */
    private $db;
    /** @var \OpenEMR\Common\Logging\SystemLogger */
    private $auditLogger;

    public function __construct($db, $auditLogger)
    {
        $this->db          = $db;
        $this->auditLogger = $auditLogger;
    }

    /**
     * Assert that $userId is authorized to access records for $patientId.
     *
     * @param string $userId    Authenticated user identifier.
     * @param string $patientId Resolved canonical patient identifier.
     * @return PanelAuthorizationToken  Opaque token proving authorization.
     *
     * @throws UnauthorizedPanelAccessException if no panel relationship exists.
     * @throws \InvalidArgumentException         if either argument is empty.
     */
    public function assertAuthorized(string $userId, string $patientId): PanelAuthorizationToken
    {
        if (empty($userId) || empty($patientId)) {
            throw new \InvalidArgumentException(
                'PanelAuthorizationService: userId and patientId must both be non-empty.'
            );
        }

        if (!$this->isPanelMember($userId, $patientId)) {
            // Write to the security audit trail BEFORE throwing.
            $this->auditLogger->warning(
                '[VUL-0017][SECURITY] Unauthorized patient record access attempt blocked',
                [
                    'user_id'   => $userId,
                    // Do NOT log patient_id to minimize PHI exposure in logs.
                    'event'     => 'panel_authorization_denied',
                    'timestamp' => (new \DateTimeImmutable())->format(\DateTime::ATOM),
                ]
            );

            throw new UnauthorizedPanelAccessException(
                'User does not have panel authorization for the requested patient.'
            );
        }

        return new PanelAuthorizationToken($userId, $patientId);
    }

    /**
     * Synchronous check: is $userId a panel member / care-team member for $patientId?
     *
     * Queries the panel_membership table which maps provider/user IDs to
     * their authorized patient panel. This is the authoritative source —
     * user-supplied context cannot override this check.
     *
     * @param string $userId
     * @param string $patientId
     * @return bool
     */
    public function isPanelMember(string $userId, string $patientId): bool
    {
        if (empty($userId) || empty($patientId)) {
            return false;
        }

        // Parameterized query — no interpolation of user-controlled values.
        $sql = '
            SELECT 1
            FROM claid_panel_membership
            WHERE user_id    = ?
              AND patient_id = ?
              AND active     = 1
            LIMIT 1
        ';

        try {
            $stmt = $this->db->prepare($sql);
            $stmt->execute([$userId, $patientId]);
            return (bool)$stmt->fetchColumn();
        } catch (\Throwable $e) {
            // Fail closed: if the panel check cannot be completed, deny access.
            $this->auditLogger->error(
                '[VUL-0017] Panel membership check failed — denying access (fail-closed)',
                ['user_id' => $userId, 'error' => $e->getMessage()]
            );
            return false;
        }
    }
}
