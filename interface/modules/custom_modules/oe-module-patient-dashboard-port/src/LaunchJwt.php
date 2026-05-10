<?php

/**
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Patient Dashboard Port Contributors
 * @copyright Copyright (c) 2026 Patient Dashboard Port Contributors
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\PatientDashboardPort;

final class LaunchJwt
{
    private const TTL_SECONDS = 60;

    public static function mint(int $userId, int $pid, string $secret): string
    {
        $header = ['alg' => 'HS256', 'typ' => 'JWT'];
        $now = time();
        $payload = [
            'sub' => (string) $userId,
            'pid' => $pid,
            'iat' => $now,
            'exp' => $now + self::TTL_SECONDS,
            'jti' => bin2hex(random_bytes(16)),
        ];
        $encodedHeader = self::base64UrlEncode(json_encode($header, JSON_THROW_ON_ERROR));
        $encodedPayload = self::base64UrlEncode(json_encode($payload, JSON_THROW_ON_ERROR));
        $signingInput = $encodedHeader . '.' . $encodedPayload;
        $signature = hash_hmac('sha256', $signingInput, $secret, true);
        return $signingInput . '.' . self::base64UrlEncode($signature);
    }

    private static function base64UrlEncode(string $data): string
    {
        return rtrim(strtr(base64_encode($data), '+/', '-_'), '=');
    }
}
