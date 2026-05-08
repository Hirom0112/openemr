<?php

/**
 * Clinical Co-Pilot — Document Download Controller.
 *
 * Read-counterpart to UploadController. Streams the bytes of a previously
 * ingested document (rows in the OpenEMR ``documents`` table) back to the
 * agent-api over a JWT-authenticated channel so the React review panel can
 * render the original artifact (PDF / PNG / JPEG / DOCX) alongside the
 * extracted facts. Bypasses the FHIR ``Binary`` GET path which is not
 * registered on this OpenEMR build (returns 404 even for stored documents).
 *
 * Auth model
 * ----------
 * Verifies the same HS256 JWT that ``UploadController.php`` accepts (shared
 * secret ``COPILOT_JWT_SECRET``, issuer ``openemr-copilot``). Server-to-server
 * only — never wired to a session or browser cookie.
 *
 * Privacy
 * -------
 * Never logs file bytes, full URL, or PHI free text. Only document id,
 * mimetype, and size cross the log boundary.
 *
 * Status codes
 * ------------
 * 200 — success, bytes streamed inline.
 * 400 — missing or non-numeric ``id`` query parameter.
 * 401 — missing/invalid JWT.
 * 404 — document id not found, deleted, or storage file missing.
 * 405 — non-GET method.
 * 500 — unexpected error reading from the document store.
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

final class DownloadController
{
    private const ISSUER         = 'openemr-copilot';
    private const ALGORITHM      = 'HS256';
    private const MIN_SECRET_LEN = 32;

    /**
     * Entry point. Reads ``$_SERVER`` / ``$_GET``, writes either a streaming
     * binary response or a JSON error envelope, and exits.
     */
    public static function handle(): void
    {
        try {
            if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'GET') {
                self::respondJson(405, ['error' => 'method_not_allowed']);
                return;
            }

            $claims = self::verifyJwt();
            if ($claims === null) {
                self::respondJson(401, ['error' => 'unauthorized']);
                return;
            }

            $rawId = (string) ($_GET['id'] ?? '');
            if ($rawId === '' || !ctype_digit($rawId)) {
                self::respondJson(400, ['error' => 'missing_id']);
                return;
            }
            $documentId = (int) $rawId;
            if ($documentId <= 0) {
                self::respondJson(400, ['error' => 'missing_id']);
                return;
            }

            $doc = new \Document($documentId);
            // Document::__construct populates from the row when the id matches.
            // get_id() returns null/empty for a row that never loaded.
            $loadedId = (int) $doc->get_id();
            if ($loadedId !== $documentId) {
                self::respondJson(404, ['error' => 'doc_not_found']);
                return;
            }
            // Defensive: deleted / expired rows should look like 404 to callers.
            if (method_exists($doc, 'is_deleted') && $doc->is_deleted()) {
                self::respondJson(404, ['error' => 'doc_not_found']);
                return;
            }
            if (method_exists($doc, 'has_expired') && $doc->has_expired()) {
                self::respondJson(404, ['error' => 'doc_not_found']);
                return;
            }

            $mimetype = trim((string) $doc->get_mimetype());
            if ($mimetype === '') {
                $mimetype = 'application/octet-stream';
            }

            try {
                $data = $doc->get_data();
            } catch (Throwable $e) {
                error_log(sprintf(
                    '[clinical-copilot] download failed: doc_id=%d err=%s',
                    $documentId,
                    $e->getMessage()
                ));
                self::respondJson(500, ['error' => 'internal_error']);
                return;
            }

            if ($data === false || $data === '' || $data === null) {
                error_log(sprintf(
                    '[clinical-copilot] download empty: doc_id=%d',
                    $documentId
                ));
                self::respondJson(404, ['error' => 'doc_not_found']);
                return;
            }

            $size = strlen($data);

            // Stream — Content-Type comes from the documents table, never user
            // input. Disposition: inline so the browser renders rather than
            // forces a download (the agent-api in turn re-streams to the iframe).
            header('Content-Type: ' . $mimetype);
            header('Content-Length: ' . $size);
            header('Content-Disposition: inline');
            header('Cache-Control: private, max-age=60');
            http_response_code(200);

            error_log(sprintf(
                '[clinical-copilot] download ok: doc_id=%d mime=%s size=%d',
                $documentId,
                $mimetype,
                $size
            ));

            echo $data;
        } catch (Throwable $e) {
            error_log('[clinical-copilot] download exception: ' . $e->getMessage());
            self::respondJson(500, ['error' => 'internal_error']);
        }
    }

    /**
     * Verify the Authorization Bearer JWT. Returns claims on success or null.
     *
     * Mirrors UploadController::verifyJwt — same secret env var, same issuer
     * check, same case-insensitive header lookup so deploys behind Apache
     * vs. nginx-fpm see consistent behavior.
     *
     * @return array<string, mixed>|null
     */
    private static function verifyJwt(): ?array
    {
        $secret = getenv('COPILOT_JWT_SECRET');
        if (!is_string($secret) || strlen($secret) < self::MIN_SECRET_LEN) {
            error_log('[clinical-copilot] download rejected: COPILOT_JWT_SECRET missing/short');
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
            error_log('[clinical-copilot] download jwt decode failed: ' . $e->getMessage());
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
     * @param array<string, mixed> $payload
     */
    private static function respondJson(int $status, array $payload): void
    {
        http_response_code($status);
        header('Content-Type: application/json');
        echo json_encode($payload, JSON_UNESCAPED_SLASHES) ?: '{}';
    }
}
