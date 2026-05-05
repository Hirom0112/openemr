<?php

/**
 * Clinical Co-Pilot — FHIR Observation upload controller.
 *
 * Custom JWT-authenticated endpoint that accepts a FHIR R4 Observation
 * resource (JSON body) and UPSERTs it into a module-private
 * ``copilot_observations`` table. Backs the agent-api extractor's
 * "one Observation per LabValue" provenance step (W2 §5.3) — OpenEMR's
 * deployed FHIR layer does not implement Observation write
 * (POST /apis/default/fhir/Observation → 404), so the agent-api routes
 * extracted lab values here instead.
 *
 * v2 (deferred): bridge into ``procedure_result`` so OpenEMR's FHIR read
 * path surfaces these Observations natively. v1 stores them in a private
 * table — the agent-api owns the read path anyway and reads back via
 * its own Postgres extraction store.
 *
 * Auth model
 * ----------
 * Same HS256 JWT shape as ``UploadController.php`` — shared secret
 * ``COPILOT_JWT_SECRET`` (>=32 chars, env var). Issuer must be
 * ``openemr-copilot``.
 *
 * Privacy
 * -------
 * Never logs raw FHIR body, raw values, citations, or PHI free text.
 * Only resource ids, document ids, and patient ids cross the log boundary.
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

final class ObservationController
{
    private const ISSUER         = 'openemr-copilot';
    private const ALGORITHM      = 'HS256';
    private const MIN_SECRET_LEN = 32;
    private const MAX_BYTES      = 256 * 1024;          // 256 KB — Observation JSON is small
    private const ID_PATTERN     = '/^copilot-\d+-[\w.-]+$/';
    private const DOCREF_PATTERN = '/^DocumentReference\/copilot-(\d+)$/';

    /** Marker used in logs only; raw values/citations never leave the request. */
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

            if (($resource['resourceType'] ?? '') !== 'Observation') {
                self::respond(400, ['error' => 'unsupported_resource_type']);
                return;
            }

            $id = isset($resource['id']) ? (string) $resource['id'] : '';
            if ($id === '' || preg_match(self::ID_PATTERN, $id) !== 1) {
                self::respond(400, ['error' => 'invalid_id']);
                return;
            }

            $derivedFrom = $resource['derivedFrom'] ?? null;
            if (!is_array($derivedFrom) || !isset($derivedFrom[0]['reference'])) {
                self::respond(400, ['error' => 'missing_derived_from']);
                return;
            }
            $docRef = (string) $derivedFrom[0]['reference'];
            if (preg_match(self::DOCREF_PATTERN, $docRef, $m) !== 1) {
                self::respond(400, ['error' => 'invalid_derived_from']);
                return;
            }
            $documentId = (int) $m[1];

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

            $coding = $resource['code']['coding'][0] ?? null;
            if (!is_array($coding) || !isset($coding['code'])) {
                self::respond(400, ['error' => 'missing_code']);
                return;
            }
            $loincCode    = (string) $coding['code'];
            $loincDisplay = isset($coding['display']) ? (string) $coding['display'] : null;

            // Value extraction. valueQuantity preferred; valueString fallback.
            $valueString  = null;
            $valueNumeric = null;
            $unit         = null;
            $vq = $resource['valueQuantity'] ?? null;
            if (is_array($vq)) {
                if (isset($vq['value'])) {
                    $valueNumeric = is_numeric($vq['value']) ? (float) $vq['value'] : null;
                    $valueString  = (string) $vq['value'];
                }
                if (isset($vq['unit'])) {
                    $unit = (string) $vq['unit'];
                }
            } elseif (isset($resource['valueString'])) {
                $valueString = (string) $resource['valueString'];
            }

            $effective = isset($resource['effectiveDateTime'])
                ? self::normalizeDateTime((string) $resource['effectiveDateTime'])
                : null;

            $citations = $resource['_copilot_citations'] ?? null;
            $citationsJson = is_array($citations)
                ? (json_encode($citations, JSON_UNESCAPED_SLASHES) ?: '[]')
                : null;

            // Strip the private extension before persisting the FHIR body
            // (keep persisted body strictly conformant; citations live in
            // their own column).
            unset($resource['_copilot_citations']);
            $fhirJson = json_encode($resource, JSON_UNESCAPED_SLASHES);
            if ($fhirJson === false) {
                self::respond(400, ['error' => 'invalid_json_serialise']);
                return;
            }

            self::ensureTable();

            $existing = sqlQuery(
                "SELECT id FROM copilot_observations WHERE id = ?",
                [$id]
            );
            $action = (is_array($existing) && isset($existing['id'])) ? 'updated' : 'created';

            // UPSERT — primary key is the deterministic resource id, so
            // re-extraction overwrites the same row.
            sqlStatement(
                "INSERT INTO copilot_observations
                    (id, document_id, patient_id, loinc_code, loinc_display,
                     value_string, value_numeric, unit, effective_date,
                     fhir_resource, citations)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                 ON DUPLICATE KEY UPDATE
                    document_id    = VALUES(document_id),
                    patient_id     = VALUES(patient_id),
                    loinc_code     = VALUES(loinc_code),
                    loinc_display  = VALUES(loinc_display),
                    value_string   = VALUES(value_string),
                    value_numeric  = VALUES(value_numeric),
                    unit           = VALUES(unit),
                    effective_date = VALUES(effective_date),
                    fhir_resource  = VALUES(fhir_resource),
                    citations      = VALUES(citations)",
                [
                    $id,
                    $documentId,
                    $patientId,
                    $loincCode,
                    $loincDisplay,
                    $valueString,
                    $valueNumeric,
                    $unit,
                    $effective,
                    $fhirJson,
                    $citationsJson,
                ]
            );

            error_log(sprintf(
                '[clinical-copilot] observation %s: id=%s doc_id=%d pid=%d loinc=%s',
                $action,
                $id,
                $documentId,
                $patientId,
                $loincCode
            ));

            self::respond(200, [
                'id'          => $id,
                'document_id' => $documentId,
                'patient_id'  => $patientId,
                'action'      => $action,
            ]);
        } catch (Throwable $e) {
            error_log('[clinical-copilot] observation exception: ' . $e->getMessage());
            self::respond(500, ['error' => 'internal_error']);
        }
    }

    /**
     * @return array<string, mixed>|null
     */
    private static function verifyJwt(): ?array
    {
        $secret = getenv('COPILOT_JWT_SECRET');
        if (!is_string($secret) || strlen($secret) < self::MIN_SECRET_LEN) {
            error_log('[clinical-copilot] observation rejected: COPILOT_JWT_SECRET missing/short');
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
            error_log('[clinical-copilot] observation jwt decode failed: ' . $e->getMessage());
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
        sqlStatement(
            "CREATE TABLE IF NOT EXISTS copilot_observations (
                id              VARCHAR(128) PRIMARY KEY,
                document_id     INT NOT NULL,
                patient_id      INT NOT NULL,
                loinc_code      VARCHAR(64) NOT NULL,
                loinc_display   VARCHAR(255),
                value_string    VARCHAR(255),
                value_numeric   DECIMAL(20,6),
                unit            VARCHAR(64),
                effective_date  DATETIME,
                fhir_resource   JSON NOT NULL,
                citations       JSON,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_document_id (document_id),
                INDEX idx_patient_id (patient_id)
            )"
        );
        self::$tableEnsured = true;
    }

    /**
     * Convert an ISO-8601 timestamp into MySQL DATETIME ("YYYY-MM-DD HH:MM:SS").
     * Returns null when the input is not parseable so the column stays NULL
     * rather than coercing a bad string in.
     */
    private static function normalizeDateTime(string $iso): ?string
    {
        try {
            $dt = new \DateTimeImmutable($iso);
            return $dt->format('Y-m-d H:i:s');
        } catch (Throwable $e) {
            return null;
        }
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
