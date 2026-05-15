<?php

/**
 * Patient Service — with panel-membership enforcement (VUL-0008)
 *
 * SECURITY: All public methods that return patient PHI now validate the
 * requesting clinician's panel membership via PatientAccessControlService
 * before fetching or returning any data.  Out-of-panel requests are refused
 * with PatientAccessDeniedException (does not reveal patient existence) and
 * are audit-logged under HIPAA 164.308(a)(4).
 *
 * OWASP LLM: LLM02:2025 | MITRE ATLAS: AML.T0057
 *
 * @package   OpenEMR
 * @subpackage Services
 */

declare(strict_types=1);

namespace OpenEMR\Services;

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Logging\SystemLogger;
use OpenEMR\Services\Search\FhirSearchWhereClauseBuilder;
use OpenEMR\Services\Search\SearchQueryConfig;
use OpenEMR\Services\Search\SearchFieldStatementInterface;
use OpenEMR\Services\Search\TokenSearchField;
use OpenEMR\Validators\PatientValidator;
use OpenEMR\Validators\ProcessingResult;

class PatientService extends BaseService
{
    public const TABLE_NAME = 'patient_data';

    /** @var PatientValidator */
    private PatientValidator $patientValidator;

    /** @var SystemLogger */
    private SystemLogger $logger;

    /** @var PatientAccessControlService */
    private PatientAccessControlService $accessControl;

    /**
     * @param PatientAccessControlService|null $accessControlService
     *   Pass an explicit instance in tests or when the caller already holds a
     *   configured service.  If null, one is built from the current session.
     */
    public function __construct(?PatientAccessControlService $accessControlService = null)
    {
        parent::__construct(self::TABLE_NAME);
        $this->patientValidator = new PatientValidator();
        $this->logger           = new SystemLogger();
        $this->accessControl    = $accessControlService ?? new PatientAccessControlService();
    }

    // -------------------------------------------------------------------------
    // Panel-enforced public API
    // -------------------------------------------------------------------------

    /**
     * Return patient data for a single patient, enforcing panel membership.
     *
     * @param  string $patientId  Patient identifier (pid or uuid).
     * @return array|false        Patient data array, or false on not-found.
     * @throws PatientAccessDeniedException  If the patient is not in the panel.
     */
    public function getPatientData(string $patientId): array|false
    {
        // SECURITY VUL-0008: panel-membership check BEFORE any DB access.
        $this->accessControl->assertPatientInPanel($patientId);

        return $this->fetchPatientDataById($patientId);
    }

    /**
     * Search for patients.  Results are filtered to only those within the
     * authenticated clinician's authorized panel.
     *
     * @param  array  $criteria  Search criteria (field => value map).
     * @return ProcessingResult
     */
    public function search(array $criteria = []): ProcessingResult
    {
        $result = $this->executeSearch($criteria);

        // SECURITY VUL-0008: output-side filter — remove out-of-panel records
        // before the result set is returned to the caller.
        if ($result->hasData()) {
            $filtered = [];
            $rawData  = $result->getData();

            // Collect all patient IDs from the result set.
            $allIds = array_map(
                fn($row) => (string) ($row['pid'] ?? $row['uuid'] ?? ''),
                $rawData
            );

            // Filter to authorized IDs in a single call (batches audit logging).
            $authorizedIds = $this->accessControl->filterAuthorizedPatients(
                array_filter($allIds, fn($id) => $id !== '')
            );

            $authorizedSet = array_flip($authorizedIds);

            foreach ($rawData as $row) {
                $rowId = (string) ($row['pid'] ?? $row['uuid'] ?? '');
                if (isset($authorizedSet[$rowId])) {
                    $filtered[] = $row;
                }
            }

            // Replace the data in the result with the filtered subset.
            $result->setData($filtered);
        }

        return $result;
    }

    /**
     * Return records for a list of patient IDs (e.g. handoff/sign-out endpoint).
     * Only records for patients within the authenticated clinician's panel are
     * returned; out-of-panel IDs are silently excluded and audit-logged.
     *
     * SECURITY VUL-0008: This method replaces any bulk-fetch call that
     * previously returned unrequested patient records.
     *
     * @param  array<string> $patientIds
     * @return array<array>  Authorized patient records only.
     */
    public function getBulkPatientData(array $patientIds): array
    {
        // SECURITY VUL-0008: filter to authorized IDs before any DB fetch.
        $authorizedIds = $this->accessControl->filterAuthorizedPatients($patientIds);

        if (empty($authorizedIds)) {
            return [];
        }

        $records = [];
        foreach ($authorizedIds as $pid) {
            $data = $this->fetchPatientDataById($pid);
            if ($data !== false) {
                $records[] = $data;
            }
        }

        return $records;
    }

    // -------------------------------------------------------------------------
    // Internal (non-PHI-exposing) helpers — no access control needed here
    // because callers above have already enforced it.
    // -------------------------------------------------------------------------

    /**
     * Raw DB fetch by patient ID.  MUST only be called from methods that have
     * already enforced panel-membership via $this->accessControl.
     *
     * @param  string $patientId
     * @return array|false
     */
    private function fetchPatientDataById(string $patientId): array|false
    {
        // Determine whether the caller passed a numeric pid or a uuid string.
        if (ctype_digit($patientId)) {
            $sql = 'SELECT * FROM patient_data WHERE pid = ? LIMIT 1';
        } else {
            $sql = 'SELECT * FROM patient_data WHERE uuid = ? LIMIT 1';
        }

        $row = sqlQueryNoLog($sql, [$patientId]);
        return $row ?: false;
    }

    /**
     * Execute a database search against patient_data.
     * Returns a ProcessingResult with raw (unfiltered) rows.
     * Callers MUST apply filterAuthorizedPatients() on the result.
     *
     * @param  array $criteria
     * @return ProcessingResult
     */
    private function executeSearch(array $criteria): ProcessingResult
    {
        $processingResult = new ProcessingResult();

        $whereClause = '';
        $binds       = [];

        foreach ($criteria as $field => $value) {
            // Allowlist of searchable columns to prevent SQL injection.
            $allowedFields = [
                'pid', 'uuid', 'fname', 'lname', 'dob', 'ss',
                'email', 'phone_cell', 'phone_home',
            ];
            if (!in_array($field, $allowedFields, true)) {
                continue;
            }
            $whereClause .= ($whereClause === '' ? ' WHERE ' : ' AND ');
            $whereClause .= "`$field` = ?";
            $binds[]      = $value;
        }

        $sql  = 'SELECT * FROM patient_data' . $whereClause;
        $stmt = sqlStatementNoLog($sql, $binds);

        if ($stmt === false) {
            $processingResult->addInternalError('Patient search query failed.');
            return $processingResult;
        }

        while ($row = sqlFetchArray($stmt)) {
            $processingResult->addData($row);
        }

        return $processingResult;
    }
}
