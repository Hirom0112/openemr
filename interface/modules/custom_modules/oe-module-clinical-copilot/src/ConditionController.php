<?php

/**
 * Clinical Co-Pilot — FHIR Condition upload controller.
 *
 * Custom JWT-authenticated endpoint that accepts a FHIR R4 Condition
 * resource (JSON body) and UPSERTs it into a module-private
 * ``copilot_conditions`` table. Backs the agent-api extractor's
 * "one Condition per ProblemListItem with grounded ICD-10" provenance
 * step (W2 §5.3) — OpenEMR's deployed FHIR layer does not implement
 * Condition write (POST /apis/default/fhir/Condition → 404 in this
 * build), so the agent-api routes extracted problem-list rows here
 * instead.
 *
 * Companion to ``ObservationController.php`` — same JWT shape, same
 * id pattern, same UPSERT discipline, same lazy-table-create pattern.
 * The two controllers share ``COPILOT_JWT_SECRET`` (>=32 chars, env
 * var) and the ``openemr-copilot`` issuer claim.
 *
 * Privacy
 * -------
 * Never logs raw FHIR body, raw condition text, or PHI free text.
 * Only resource ids, document ids, patient ids, and ICD-10 / SNOMED
 * codes cross the log boundary.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot Contributors
 * @copyright Copyright (c) 2026 Clinical Co-Pilot Contributors
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCopilot;

use Firebase\JWT\JWT;
use Firebase\JWT\Key;
use Throwable;

final class ConditionController
{
    private const ISSUER         = 'openemr-copilot';
    private const ALGORITHM      = 'HS256';
    private const MIN_SECRET_LEN = 32;
    private const MAX_BYTES      = 256 * 1024;          // 256 KB — Condition JSON is small
    private const ID_PATTERN     = '/^copilot-\d+-[\w.-]+$/';
    private const DOCREF_PATTERN = '/^DocumentReference\/copilot-(\d+)$/';

    private const ICD10_SYSTEM   = 'http://hl7.org/fhir/sid/icd-10-cm';
    private const SNOMED_SYSTEM  = 'http://snomed.info/sct';

    private const ALLOWED_CLINICAL_STATUS = ['active', 'resolved', 'inactive'];
    private const ALLOWED_VERIFICATION_STATUS = [
        'unconfirmed',
        'provisional',
        'differential',
        'confirmed',
        'refuted',
        'entered-in-error',
    ];

    private static bool $tableEnsured = false;

    public static function handle(): void
    {
        header('Content-Type: application/json');

        try {
            if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'POST') {
                self::respond(405, ['error' => 'method_not_allowed']);
                return;
            }

            $claims = self::verifyJwt();
            if ($claims === null) {
                self::respond(401, ['error' => 'unauthorized']);
                return;
            }

            $rawBody = (string) file_get_contents('php://input');
            if ($rawBody === '' || strlen($rawBody) > self::MAX_BYTES) {
                self::respond(400, ['error' => 'invalid_body_size']);
                return;
            }

            $resource = json_decode($rawBody, true);
            if (!is_array($resource)) {
                self::respond(400, ['error' => 'invalid_json']);
                return;
            }

            if (($resource['resourceType'] ?? '') !== 'Condition') {
                self::respond(400, ['error' => 'unsupported_resource_type']);
                return;
            }

            $id = isset($resource['id']) ? (string) $resource['id'] : '';
            if ($id === '' || preg_match(self::ID_PATTERN, $id) !== 1) {
                self::respond(400, ['error' => 'invalid_id']);
                return;
            }

            // Subject — Patient/{numeric pid}, mirroring Observation.
            $subjectRef = (string) ($resource['subject']['reference'] ?? '');
            if (!preg_match('/^Patient\/(\w+)$/', $subjectRef, $sm)) {
                self::respond(400, ['error' => 'invalid_subject']);
                return;
            }
            $patientId = (int) $sm[1];
            if ($patientId <= 0) {
                self::respond(400, ['error' => 'invalid_patient_id']);
                return;
            }

            // derivedFrom — copilot DocumentReference, optional but
            // strongly preferred. When absent (e.g. clinician-edited
            // row with no source link), document_id stays 0.
            $documentId = 0;
            $derivedFrom = $resource['derivedFrom'] ?? null;
            if (is_array($derivedFrom) && isset($derivedFrom[0]['reference'])) {
                $docRef = (string) $derivedFrom[0]['reference'];
                if (preg_match(self::DOCREF_PATTERN, $docRef, $dm) === 1) {
                    $documentId = (int) $dm[1];
                }
            }

            // Coding — accept any number of codings on resource.code
            // and extract the first ICD-10 + first SNOMED. Conditions
            // commonly carry both; the writer panel emits both when
            // both are grounded.
            $codings = $resource['code']['coding'] ?? [];
            if (!is_array($codings)) {
                $codings = [];
            }
            $icd10Code = null;
            $snomedCode = null;
            foreach ($codings as $coding) {
                if (!is_array($coding) || !isset($coding['code'])) {
                    continue;
                }
                $system = isset($coding['system']) ? (string) $coding['system'] : '';
                $code = (string) $coding['code'];
                if ($system === self::ICD10_SYSTEM && $icd10Code === null) {
                    $icd10Code = $code;
                } elseif ($system === self::SNOMED_SYSTEM && $snomedCode === null) {
                    $snomedCode = $code;
                }
            }
            // condition_text is required — fall back to code.text
            // (USCDI's condition.text), then to the first coding's
            // display, then to the first coding's code as a literal.
            $conditionText = '';
            if (isset($resource['code']['text']) && is_string($resource['code']['text'])) {
                $conditionText = trim((string) $resource['code']['text']);
            }
            if ($conditionText === '' && isset($codings[0]['display']) && is_string($codings[0]['display'])) {
                $conditionText = trim((string) $codings[0]['display']);
            }
            if ($conditionText === '' && isset($codings[0]['code']) && is_string($codings[0]['code'])) {
                $conditionText = trim((string) $codings[0]['code']);
            }
            if ($conditionText === '') {
                self::respond(400, ['error' => 'missing_condition_text']);
                return;
            }

            // Clinical status (USCDI required).
            $clinicalStatus = self::extractStatusCode(
                $resource['clinicalStatus'] ?? null,
                self::ALLOWED_CLINICAL_STATUS
            );
            if ($clinicalStatus === null) {
                self::respond(400, ['error' => 'invalid_clinical_status']);
                return;
            }

            // Verification status (USCDI required). Default 'unconfirmed'
            // when absent — same posture FhirConditionProblemListItemService
            // uses for problem-list rows.
            $verificationStatus = self::extractStatusCode(
                $resource['verificationStatus'] ?? null,
                self::ALLOWED_VERIFICATION_STATUS
            );
            if ($verificationStatus === null) {
                $verificationStatus = 'unconfirmed';
            }

            // onsetDateTime — accept the FHIR string verbatim if it's
            // a parseable timestamp; otherwise the optional onset_date
            // column on copilot_conditions stores the string as-is so
            // imprecise values like "~2018" survive.
            $onsetRaw = null;
            if (isset($resource['onsetDateTime']) && is_string($resource['onsetDateTime'])) {
                $onsetRaw = (string) $resource['onsetDateTime'];
            } elseif (isset($resource['onsetString']) && is_string($resource['onsetString'])) {
                $onsetRaw = (string) $resource['onsetString'];
            }

            $citations = $resource['_copilot_citations'] ?? null;
            $citationsJson = is_array($citations)
                ? (json_encode($citations, JSON_UNESCAPED_SLASHES) ?: '[]')
                : null;

            // Strip the private extension before persisting the FHIR body.
            unset($resource['_copilot_citations']);
            $fhirJson = json_encode($resource, JSON_UNESCAPED_SLASHES);
            if ($fhirJson === false) {
                self::respond(400, ['error' => 'invalid_json_serialise']);
                return;
            }

            self::ensureTable();

            $existing = sqlQuery(
                "SELECT id FROM copilot_conditions WHERE id = ?",
                [$id]
            );
            $action = (is_array($existing) && isset($existing['id'])) ? 'updated' : 'created';

            sqlStatement(
                "INSERT INTO copilot_conditions
                    (id, document_id, patient_id, icd10_code, snomed_code,
                     condition_text, onset_date,
                     clinical_status, verification_status,
                     fhir_resource, citations)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                 ON DUPLICATE KEY UPDATE
                    document_id         = VALUES(document_id),
                    patient_id          = VALUES(patient_id),
                    icd10_code          = VALUES(icd10_code),
                    snomed_code         = VALUES(snomed_code),
                    condition_text      = VALUES(condition_text),
                    onset_date          = VALUES(onset_date),
                    clinical_status     = VALUES(clinical_status),
                    verification_status = VALUES(verification_status),
                    fhir_resource       = VALUES(fhir_resource),
                    citations           = VALUES(citations)",
                [
                    $id,
                    $documentId,
                    $patientId,
                    $icd10Code,
                    $snomedCode,
                    $conditionText,
                    $onsetRaw,
                    $clinicalStatus,
                    $verificationStatus,
                    $fhirJson,
                    $citationsJson,
                ]
            );

            error_log(sprintf(
                '[clinical-copilot] condition %s: id=%s doc_id=%d pid=%d icd10=%s snomed=%s',
                $action,
                $id,
                $documentId,
                $patientId,
                $icd10Code ?? '-',
                $snomedCode ?? '-'
            ));

            self::respond(200, [
                'id'          => $id,
                'document_id' => $documentId,
                'patient_id'  => $patientId,
                'action'      => $action,
            ]);
        } catch (Throwable $e) {
            error_log('[clinical-copilot] condition exception: ' . $e->getMessage());
            self::respond(500, ['error' => 'internal_error']);
        }
    }

    /**
     * @param mixed $statusBlock the FHIR CodeableConcept payload (clinicalStatus / verificationStatus)
     * @param string[] $allowed allowed code values
     * @return string|null normalised code, or null when the block is missing/invalid
     */
    private static function extractStatusCode($statusBlock, array $allowed): ?string
    {
        if (!is_array($statusBlock)) {
            return null;
        }
        $coding = $statusBlock['coding'] ?? null;
        if (!is_array($coding) || !isset($coding[0]['code'])) {
            return null;
        }
        $code = strtolower((string) $coding[0]['code']);
        return in_array($code, $allowed, true) ? $code : null;
    }

    /**
     * @return array<string, mixed>|null
     */
    private static function verifyJwt(): ?array
    {
        $secret = getenv('COPILOT_JWT_SECRET');
        if (!is_string($secret) || strlen($secret) < self::MIN_SECRET_LEN) {
            error_log('[clinical-copilot] condition rejected: COPILOT_JWT_SECRET missing/short');
            return null;
        }

        $header = '';
        if (isset($_SERVER['HTTP_AUTHORIZATION'])) {
            $header = (string) $_SERVER['HTTP_AUTHORIZATION'];
        } elseif (function_exists('apache_request_headers')) {
            $hdrs = apache_request_headers();
            if (is_array($hdrs)) {
                foreach ($hdrs as $k => $v) {
                    if (strcasecmp((string) $k, 'Authorization') === 0) {
                        $header = (string) $v;
                        break;
                    }
                }
            }
        }

        if (stripos($header, 'Bearer ') !== 0) {
            return null;
        }
        $token = trim(substr($header, 7));
        if ($token === '') {
            return null;
        }

        try {
            $decoded = JWT::decode($token, new Key($secret, self::ALGORITHM));
            $claims  = (array) $decoded;
        } catch (Throwable $e) {
            error_log('[clinical-copilot] condition jwt decode failed: ' . $e->getMessage());
            return null;
        }

        if ((string) ($claims['iss'] ?? '') !== self::ISSUER) {
            return null;
        }
        if (!isset($claims['sub']) || (string) $claims['sub'] === '') {
            return null;
        }
        return $claims;
    }

    private static function ensureTable(): void
    {
        if (self::$tableEnsured) {
            return;
        }
        // Lazy table creation — same pattern ObservationController uses.
        // The row is keyed by deterministic copilot id (UPSERT primary
        // key) so re-extraction of the same problem on the same
        // document overwrites instead of duplicating.
        sqlStatement(
            "CREATE TABLE IF NOT EXISTS copilot_conditions (
                id                   VARCHAR(128) PRIMARY KEY,
                document_id          INT NOT NULL,
                patient_id           INT NOT NULL,
                icd10_code           VARCHAR(16),
                snomed_code          VARCHAR(32),
                condition_text       VARCHAR(255) NOT NULL,
                onset_date           VARCHAR(64),
                clinical_status      VARCHAR(32) NOT NULL,
                verification_status  VARCHAR(32) NOT NULL,
                fhir_resource        JSON NOT NULL,
                citations            JSON,
                created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_document_id (document_id),
                INDEX idx_patient_id (patient_id),
                INDEX idx_icd10 (icd10_code)
            )"
        );
        self::$tableEnsured = true;
    }

    /**
     * @param array<string, mixed> $payload
     */
    private static function respond(int $status, array $payload): void
    {
        http_response_code($status);
        echo json_encode($payload, JSON_UNESCAPED_SLASHES) ?: '{}';
    }
}
