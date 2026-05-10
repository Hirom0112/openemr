<?php

/**
 * Clinical Co-Pilot — Document listing public entry point.
 *
 * Custom JWT-authenticated endpoint that lists documents persisted by the
 * Co-Pilot ingest pipeline (category 'Clinical Copilot Upload' / LOINC
 * 34109-9). This is the v1 escape hatch for the upstream FHIR
 * `/DocumentReference?patient=<uuid>` endpoint, which on this OpenEMR build
 * returns total=0 even when documents exist for the patient. Two upstream
 * filters cause the empty Bundle:
 *
 *   1. `FhirPatientDocumentReferenceService::searchForOpenEMRRecords`
 *      (src/Services/FHIR/DocumentReference/FhirPatientDocumentReferenceService.php
 *      lines 110-118) forces `puuid IS MISSING` whenever the request omits
 *      `?patient=`, which is the anti-leak default for un-scoped Bundle reads.
 *   2. `MappedServiceTrait::searchAllServices`
 *      (src/Services/FHIR/Traits/MappedServiceTrait.php lines 47-65) fans the
 *      query out to ClinicalNotes / PatientDocument / ADI sub-services and
 *      clears all results if any one of them throws SearchFieldException.
 *
 * The architectural fix (custom mapped sub-service that bypasses both
 * filters for the Co-Pilot category) ships alongside this file as
 * `src/FHIR/FhirCopilotDocumentReferenceService.php` and is registered in
 * core via the documented exception in `agent-api/CLAUDE.md` (rule 8). This
 * endpoint stays in place as the deploy-independent path — it does not
 * require an OpenEMR core redeploy to query.
 *
 * Auth model
 * ----------
 * Verifies the same HS256 JWT that `JwtMinter.php` mints for the React
 * iframe — shared secret `COPILOT_JWT_SECRET` (>=32 chars, env var). The
 * agent-api mints an equivalent token (issuer `openemr-copilot`, fields
 * `sub` / `sid` / `iat` / `exp`) using the same secret. No `$_SESSION`
 * read; the JWT is the trust boundary.
 *
 * Endpoint contract
 * -----------------
 *   GET documents.php?patient_id=<numeric_pid>
 *   GET documents.php?patient_uuid=<uuid_hex_or_dashed>
 *
 *   200 -> {"patient_id": 12, "documents": [
 *     {"document_id": 138, "uuid": "<hex>", "name": "document.docx",
 *      "mimetype": "application/...", "date": "2026-05-08 17:37:14",
 *      "category_id": 35, "category_name": "Clinical Copilot Upload"},
 *     ...
 *   ]}
 *   400 -> {"error": "missing_patient"} | {"error": "bad_patient_uuid"}
 *   401 -> {"error": "unauthorized"}
 *   405 -> {"error": "method_not_allowed"}
 *   500 -> {"error": "internal_error"}
 *
 * Filters: only documents linked to category 'Clinical Copilot Upload'
 * (resolved by name, not by hard-coded id 35), only `documents.deleted=0`,
 * ordered by `date DESC`. Empty list (200) is returned when the patient has
 * no Co-Pilot documents.
 *
 * Privacy
 * -------
 * Never logs document bytes, JWT raw, or PHI free text. Only sizes, MIME
 * types, and patient ids cross the log boundary.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot Contributors
 * @copyright Copyright (c) 2026 Clinical Co-Pilot Contributors
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

// Tell globals.php this is a no-session API endpoint. Auth is the HS256
// JWT verified below; we deliberately bypass OpenEMR's session + CSRF
// gates (this is a server-to-server caller from the agent-api).
$ignoreAuth        = true;
$sessionAllowWrite = false;
$skipFinishLogin   = true;

require_once __DIR__ . '/../../../../globals.php';

use Firebase\JWT\JWT;
use Firebase\JWT\Key;

const COPILOT_DOCS_ISSUER         = 'openemr-copilot';
const COPILOT_DOCS_ALG            = 'HS256';
const COPILOT_DOCS_MIN_SECRET_LEN = 32;
const COPILOT_DOCS_CATEGORY_NAME  = 'Clinical Copilot Upload';
const COPILOT_DOCS_MAX_LIMIT      = 200;

header('Content-Type: application/json');

try {
    if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'GET') {
        copilot_docs_respond(405, ['error' => 'method_not_allowed']);
        return;
    }

    $claims = copilot_docs_verify_jwt();
    if ($claims === null) {
        copilot_docs_respond(401, ['error' => 'unauthorized']);
        return;
    }

    $patientId = copilot_docs_resolve_pid();
    if ($patientId === null) {
        // resolve_pid already wrote the 400 response and logged the cause.
        return;
    }

    $rows = copilot_docs_fetch($patientId);

    $documents = [];
    foreach ($rows as $row) {
        $documents[] = [
            'document_id'   => (int) $row['id'],
            'uuid'          => isset($row['uuid_hex']) && is_string($row['uuid_hex'])
                ? strtolower((string) $row['uuid_hex'])
                : null,
            'name'          => (string) ($row['name'] ?? ''),
            'mimetype'      => (string) ($row['mimetype'] ?? ''),
            'date'          => $row['date'] ?? null,
            'size'          => isset($row['size']) ? (int) $row['size'] : null,
            'category_id'   => (int) $row['category_id'],
            'category_name' => (string) ($row['category_name'] ?? ''),
        ];
    }

    error_log(sprintf(
        '[clinical-copilot] documents list ok: pid=%d count=%d',
        $patientId,
        count($documents)
    ));

    copilot_docs_respond(200, [
        'patient_id' => $patientId,
        'documents'  => $documents,
    ]);
} catch (Throwable $e) {
    error_log('[clinical-copilot] documents list exception: ' . $e->getMessage());
    copilot_docs_respond(500, ['error' => 'internal_error']);
}

/**
 * Verify the Authorization Bearer JWT. Returns claims on success or null.
 *
 * @return array<string, mixed>|null
 */
function copilot_docs_verify_jwt(): ?array
{
    $secret = getenv('COPILOT_JWT_SECRET');
    if (!is_string($secret) || strlen($secret) < COPILOT_DOCS_MIN_SECRET_LEN) {
        error_log('[clinical-copilot] documents rejected: COPILOT_JWT_SECRET missing/short');
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
        $decoded = JWT::decode($token, new Key($secret, COPILOT_DOCS_ALG));
        $claims = (array) $decoded;
    } catch (Throwable $e) {
        error_log('[clinical-copilot] documents jwt decode failed: ' . $e->getMessage());
        return null;
    }

    if ((string) ($claims['iss'] ?? '') !== COPILOT_DOCS_ISSUER) {
        return null;
    }
    if (!isset($claims['sub']) || (string) $claims['sub'] === '') {
        return null;
    }
    return $claims;
}

/**
 * Resolve the requested patient id from query params. Accepts either
 * ?patient_id=<numeric pid> or ?patient_uuid=<32-hex-or-dashed-uuid>.
 * Returns the integer pid, or null after writing a 400 response.
 */
function copilot_docs_resolve_pid(): ?int
{
    $rawId = isset($_GET['patient_id']) ? trim((string) $_GET['patient_id']) : '';
    if ($rawId !== '' && ctype_digit($rawId)) {
        $pid = (int) $rawId;
        if ($pid > 0) {
            return $pid;
        }
    }

    $rawUuid = isset($_GET['patient_uuid']) ? trim((string) $_GET['patient_uuid']) : '';
    if ($rawUuid !== '') {
        $hex = strtolower(str_replace('-', '', $rawUuid));
        if (preg_match('/^[0-9a-f]{32}$/', $hex) !== 1) {
            copilot_docs_respond(400, ['error' => 'bad_patient_uuid']);
            return null;
        }
        $row = sqlQuery(
            "SELECT pid FROM patient_data WHERE uuid = UNHEX(?) LIMIT 1",
            [$hex]
        );
        if (is_array($row) && isset($row['pid']) && (int) $row['pid'] > 0) {
            return (int) $row['pid'];
        }
        copilot_docs_respond(400, ['error' => 'unknown_patient_uuid']);
        return null;
    }

    copilot_docs_respond(400, ['error' => 'missing_patient']);
    return null;
}

/**
 * Fetch Co-Pilot-category documents for the given pid. Joins through
 * categories_to_documents (the documents table has no category_id column;
 * categories are linked via the join table, mirroring how the legacy
 * `Document::createDocument` path persists them).
 *
 * @return list<array<string, mixed>>
 */
function copilot_docs_fetch(int $pid): array
{
    $sql = "SELECT
                d.id,
                LOWER(HEX(d.uuid)) AS uuid_hex,
                d.name,
                d.mimetype,
                d.date,
                d.size,
                c.id   AS category_id,
                c.name AS category_name
            FROM documents d
            JOIN categories_to_documents c2d ON c2d.document_id = d.id
            JOIN categories c ON c.id = c2d.category_id
            WHERE d.foreign_id = ?
              AND d.deleted = 0
              AND c.name = ?
            ORDER BY d.date DESC, d.id DESC
            LIMIT " . COPILOT_DOCS_MAX_LIMIT;

    $stmt = sqlStatement($sql, [$pid, COPILOT_DOCS_CATEGORY_NAME]);
    $out = [];
    while ($row = sqlFetchArray($stmt)) {
        $out[] = $row;
    }
    return $out;
}

/**
 * @param array<string, mixed> $payload
 */
function copilot_docs_respond(int $status, array $payload): void
{
    http_response_code($status);
    echo json_encode($payload, JSON_UNESCAPED_SLASHES) ?: '{}';
}
