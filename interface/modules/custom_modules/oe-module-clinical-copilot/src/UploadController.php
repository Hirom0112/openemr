<?php

/**
 * Clinical Co-Pilot — Document Upload Controller.
 *
 * Custom JWT-authenticated endpoint that accepts a multipart document upload
 * (PDF, PNG/JPEG/TIFF, DOCX, XLSX, HL7 v2) from the agent-api and persists
 * it into OpenEMR's documents table + filesystem via the legacy
 * ``Document::createDocument`` API. Bypasses the FHIR Binary endpoint
 * (read-only on this OpenEMR build) and the legacy REST
 * ``/apis/default/api/patient/{pid}/document`` endpoint (returns 401 even
 * with a valid bearer + api:oemr scope on this deploy).
 *
 * MIME validation
 * ---------------
 * Trusts the client-declared MIME from the multipart part header
 * (``$_FILES['file']['type']``) rather than libmagic
 * (``mime_content_type``). libmagic returns ``application/zip`` for XLSX/DOCX
 * (they're zip containers) and ``text/plain`` for HL7 v2, so it cannot
 * distinguish the multimodal formats agent-api sends. The HS256 JWT gate
 * (issuer ``openemr-copilot``, shared ``COPILOT_JWT_SECRET``) is the real
 * trust boundary; a forged client cannot reach this code path.
 *
 * Auth model
 * ----------
 * Verifies the same HS256 JWT that ``JwtMinter.php`` mints for the React
 * iframe — shared secret ``COPILOT_JWT_SECRET`` (>=32 chars, env var). The
 * agent-api mints an equivalent token (issuer ``openemr-copilot``, fields
 * ``sub`` / ``sid`` / ``iat`` / ``exp``) using the same secret.
 *
 * Privacy
 * -------
 * Never logs PDF bytes, base64 payload, JWT raw, or PHI free text. Only
 * sizes, MIME types, and patient ids cross the log boundary.
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

final class UploadController
{
    private const ISSUER          = 'openemr-copilot';
    private const ALGORITHM       = 'HS256';
    private const MIN_SECRET_LEN  = 32;
    private const MAX_BYTES       = 25 * 1024 * 1024;   // 25 MB
    private const ALLOWED_MIMES   = [
        'application/pdf',
        'image/png',
        'image/jpeg',
        'image/tiff',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/hl7-v2',
    ];
    private const DEFAULT_CATEGORY      = 'Clinical Copilot Upload';
    private const DEFAULT_CATEGORY_CODE = 'LOINC:34109-9';
    private const DEFAULT_CATEGORY_PARENT = 1;

    /**
     * Entry point. Reads $_SERVER / $_POST / $_FILES, writes a JSON response,
     * and exits. Designed to be invoked from a thin public/*.php shim.
     */
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

            $patientId = trim((string) ($_POST['patient_id'] ?? ''));
            if ($patientId === '') {
                self::respond(400, ['error' => 'missing_patient_id']);
                return;
            }

            $file = $_FILES['file'] ?? null;
            if (
                !is_array($file)
                || !isset($file['tmp_name'], $file['error'], $file['size'], $file['name'])
                || $file['error'] !== UPLOAD_ERR_OK
                || !is_uploaded_file((string) $file['tmp_name'])
            ) {
                self::respond(400, ['error' => 'missing_file']);
                return;
            }

            $size = (int) $file['size'];
            if ($size <= 0 || $size > self::MAX_BYTES) {
                self::respond(400, ['error' => 'file_too_large']);
                return;
            }

            $tmpName = (string) $file['tmp_name'];
            // Trust the client-declared MIME (set by the JWT-authenticated
            // agent-api in the multipart part header). libmagic-based
            // mime_content_type cannot distinguish XLSX/DOCX from generic
            // application/zip, nor HL7 v2 from text/plain.
            $mime = strtolower(trim((string) ($file['type'] ?? '')));
            if ($mime === '' || !in_array($mime, self::ALLOWED_MIMES, true)) {
                self::respond(400, ['error' => 'unsupported_mime', 'mime' => $mime]);
                return;
            }

            $data = @file_get_contents($tmpName);
            if ($data === false || $data === '') {
                self::respond(400, ['error' => 'empty_file']);
                return;
            }

            $filename = self::sanitizeFilename((string) $file['name']);
            $docTypeHint = isset($_POST['doc_type_hint'])
                ? trim((string) $_POST['doc_type_hint'])
                : '';

            $categoryId = self::resolveCategoryId();

            $doc = new \Document();
            $createErr = $doc->createDocument(
                $patientId,
                $categoryId,
                $filename,
                strtolower($mime),
                $data,
                '',
                1,
                (int) ($claims['sub'] ?? 0),
                $tmpName
            );

            if (!empty($createErr)) {
                error_log(sprintf(
                    '[clinical-copilot] upload failed: pid=%s size=%d err=%s',
                    $patientId,
                    $size,
                    is_string($createErr) ? $createErr : 'unknown'
                ));
                self::respond(500, ['error' => 'document_create_failed']);
                return;
            }

            $documentId = (int) $doc->get_id();
            error_log(sprintf(
                '[clinical-copilot] upload ok: pid=%s doc_id=%d size=%d hint=%s',
                $patientId,
                $documentId,
                $size,
                $docTypeHint !== '' ? $docTypeHint : 'none'
            ));

            self::respond(200, [
                'documentId' => $documentId,
                'patient_id' => $patientId,
                'category_id' => $categoryId,
            ]);
        } catch (Throwable $e) {
            error_log('[clinical-copilot] upload exception: ' . $e->getMessage());
            self::respond(500, ['error' => 'internal_error']);
        }
    }

    /**
     * Verify the Authorization Bearer JWT. Returns claims on success or null.
     *
     * @return array<string, mixed>|null
     */
    private static function verifyJwt(): ?array
    {
        $secret = getenv('COPILOT_JWT_SECRET');
        if (!is_string($secret) || strlen($secret) < self::MIN_SECRET_LEN) {
            error_log('[clinical-copilot] upload rejected: COPILOT_JWT_SECRET missing/short');
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
            $claims = (array) $decoded;
        } catch (Throwable $e) {
            error_log('[clinical-copilot] jwt decode failed: ' . $e->getMessage());
            return null;
        }

        $iss = (string) ($claims['iss'] ?? '');
        if ($iss !== self::ISSUER) {
            return null;
        }
        if (!isset($claims['sub']) || (string) $claims['sub'] === '') {
            return null;
        }

        return $claims;
    }

    /**
     * Resolve the ``Clinical Copilot Upload`` category id, creating it on
     * first use if the row does not yet exist. Categorises uploaded docs
     * under a category whose ``codes`` column carries LOINC ``34109-9``
     * ("Note") so that the FHIR DocumentReference search surfaces them
     * (DocumentService joins ``categories.codes`` into the result row;
     * categories without a code resolve to a Data Absent Reason and are
     * filtered out by FHIR consumers).
     *
     * Falls back to the first top-level category if the create-on-first-use
     * INSERT fails (e.g. read-only DB user), and finally to 1 if the
     * categories table is empty entirely. ``Document::createDocument`` will
     * still write the document row in any case.
     */
    private static function resolveCategoryId(): int
    {
        $row = sqlQuery(
            "SELECT id FROM categories WHERE name = ? ORDER BY id ASC LIMIT 1",
            [self::DEFAULT_CATEGORY]
        );
        if (is_array($row) && isset($row['id'])) {
            return (int) $row['id'];
        }

        // Create-on-first-use: insert a dedicated category whose codes
        // column carries the LOINC code that the FHIR DocumentReference
        // search needs. The categories table has no auto-increment, so
        // we allocate the next id from MAX(id)+1 and keep categories_seq
        // in sync (legacy CategoryTree code reads from it). Idempotent in
        // practice because the SELECT above runs first; on a race a
        // second row with the same name is benign — the next caller's
        // SELECT picks whichever id wins.
        try {
            $nextRow = sqlQuery("SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM categories");
            $nextId = (is_array($nextRow) && isset($nextRow['next_id'])) ? (int) $nextRow['next_id'] : 1;
            sqlStatement(
                "INSERT INTO categories (id, name, value, parent, lft, rght, aco_spec, codes)"
                . " VALUES (?, ?, '', ?, 0, 0, 'patients|docs', ?)",
                [$nextId, self::DEFAULT_CATEGORY, self::DEFAULT_CATEGORY_PARENT, self::DEFAULT_CATEGORY_CODE]
            );
            // Keep the legacy sequence table aligned for any code that
            // reads it; ignored if the table doesn't exist on this build.
            try {
                sqlStatement(
                    "UPDATE categories_seq SET id = (SELECT MAX(id) FROM categories)"
                );
            } catch (Throwable $seqErr) {
                // intentional no-op: categories_seq is informational here
            }
            return $nextId;
        } catch (Throwable $e) {
            error_log('[clinical-copilot] category create failed: ' . $e->getMessage());
        }

        $fallback = sqlQuery(
            "SELECT id FROM categories WHERE parent = 0 ORDER BY id ASC LIMIT 1"
        );
        if (is_array($fallback) && isset($fallback['id'])) {
            return (int) $fallback['id'];
        }
        return 1;
    }

    private static function sanitizeFilename(string $raw): string
    {
        $base = basename($raw);
        $base = preg_replace('/[^A-Za-z0-9._\-]+/', '_', $base) ?? 'document';
        if ($base === '' || $base === '.' || $base === '..') {
            $base = 'document';
        }
        return $base;
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
