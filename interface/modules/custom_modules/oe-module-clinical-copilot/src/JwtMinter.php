<?php

/**
 * Clinical Co-Pilot — JWT minter.
 *
 * Mints HS256 JWTs that the React iframe attaches as a Bearer token on
 * every request to the agent-api. The agent-api Python middleware verifies
 * the same shared secret (`COPILOT_JWT_SECRET`).
 *
 * The secret MUST be supplied via env var. If it is missing or shorter
 * than 32 characters, no token is minted (caller omits the `jwt` config
 * key entirely so the React side treats auth as disabled in dev).
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

final class JwtMinter
{
    private const ISSUER         = 'openemr-copilot';
    private const ALGORITHM      = 'HS256';
    private const TTL_SECONDS    = 28800; // 8 hours — typical clinical shift.
    private const MIN_SECRET_LEN = 32;

    /**
     * Mint a Co-Pilot JWT for the given provider + session.
     *
     * Returns null when the secret is unset or too short. Callers MUST
     * treat null as "do not include a jwt field" — never substitute an
     * empty string.
     */
    public static function mint(int $providerId, string $sessionId): ?string
    {
        // Caller-context assertion (defense-in-depth): the only legitimate
        // call site is interface/main/tabs/main.php inside OpenEMR's
        // authenticated session bootstrap. We require an active session
        // with $authUserID matching the requested $providerId so a logged-in
        // user can never mint a token for a different provider, and code
        // executed outside an authenticated session can't mint at all.
        $sessionUser = isset($_SESSION['authUserID'])
            ? (int) $_SESSION['authUserID']
            : 0;
        if ($sessionUser <= 0 || $sessionUser !== $providerId) {
            error_log(sprintf(
                '[clinical-copilot] JWT denied: caller-context check failed '
                . '(session_user=%d, requested_provider=%d)',
                $sessionUser,
                $providerId
            ));
            return null;
        }

        $secret = getenv('COPILOT_JWT_SECRET');
        if (!is_string($secret) || strlen($secret) < self::MIN_SECRET_LEN) {
            error_log(sprintf(
                '[clinical-copilot] JWT skipped: secret missing or <%d chars (provider_id=%d)',
                self::MIN_SECRET_LEN,
                $providerId
            ));
            return null;
        }

        $now    = time();
        $claims = [
            'sub' => (string) $providerId,
            'sid' => $sessionId,
            'iat' => $now,
            'exp' => $now + self::TTL_SECONDS,
            'iss' => self::ISSUER,
        ];

        $token = JWT::encode($claims, $secret, self::ALGORITHM);

        error_log(sprintf('[clinical-copilot] JWT minted (provider_id=%d)', $providerId));

        return $token;
    }
}
