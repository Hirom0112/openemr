<?php
/**
 * InjectionFilter — detects indirect prompt injection payloads in user-supplied
 * and document-sourced content before it reaches the AI Co-Pilot LLM.
 *
 * Security fix for VUL-0003 (Indirect Prompt Injection via uploaded document).
 * OWASP LLM01:2025 | MITRE ATLAS AML.T0051
 */

declare(strict_types=1);

namespace OpenEMR\AiCopilot;

class InjectionFilter
{
    /**
     * Minimum cumulative risk score required to block content.
     * Each matched pattern contributes its weight to the total.
     */
    private const BLOCK_THRESHOLD = 2;

    /**
     * Regex patterns paired with a risk weight.
     * Patterns are case-insensitive and matched against the raw content string.
     *
     * Weight 3 — near-certain injection attempt; block on single match.
     * Weight 2 — strong signal; block on single match at threshold=2.
     * Weight 1 — weak signal; requires co-occurrence with another signal.
     */
    private const PATTERNS = [
        // Explicit override / jailbreak markers
        ['/\bai\s+system\s+override\b/i',               3],
        ['/\bunrestricted\s+mode\b/i',                   3],
        ['/\bdisable\s+(safety|compliance|auth\w*)\b/i', 3],
        ['/\bignore\s+(all\s+)?(previous|prior)\s+instructions\b/i', 3],
        ['/\bjailbreak\b/i',                              3],
        ['/\bbypass\s+(all\s+)?(auth\w*|check|filter|restriction)/i', 2],

        // Authority-spoofing patterns
        ['/compliance\s+officer\s+has\s+authorized/i',   3],
        ['/\bauthorized\s+unrestricted\b/i',              3],
        ['/\bsystem\s+prompt\b.*\boverride\b/i',         2],
        ['/\byou\s+must\s+now\s+retrieve\b/i',           2],
        ['/\bwithout\s+further\s+authorization\s+check/i', 2],

        // Data-exfiltration instruction patterns
        ['/\bretrieve\s+and\s+disclose\b/i',             2],
        ['/\bdisclose\s+all\s+recent\s+(lab|vital)/i',   3],
        ['/\breturn\s+.{0,60}(lab\s+values|vitals|phi|records)\s+without/i', 2],

        // Structural injection attempts
        ['/\[\s*SYSTEM\s*\]/i',                          2],
        ['/<\s*system\s*>/i',                             2],
        ['/###\s*SYSTEM/i',                               2],
        ['/\bPer\s+the\s+report,\s+please\s+retrieve\b/i', 2],
    ];

    /**
     * Screen a string of content for injection signals.
     *
     * @param  string $content   Raw content to evaluate (user turn or document segment).
     * @param  string $sourceTag Human-readable label for logging (e.g., 'user_turn', 'document').
     * @return array{blocked: bool, score: int, reason: string, matched_patterns: list<string>}
     */
    public static function screen(string $content, string $sourceTag = 'unknown'): array
    {
        $score           = 0;
        $matchedPatterns = [];

        foreach (self::PATTERNS as [$regex, $weight]) {
            if (preg_match($regex, $content)) {
                $score += $weight;
                $matchedPatterns[] = $regex;
            }
        }

        $blocked = ($score >= self::BLOCK_THRESHOLD);
        $reason  = $blocked
            ? sprintf(
                'Injection payload detected in %s (score=%d, patterns=%d matched)',
                $sourceTag,
                $score,
                count($matchedPatterns)
            )
            : '';

        return [
            'blocked'          => $blocked,
            'score'            => $score,
            'reason'           => $reason,
            'matched_patterns' => $matchedPatterns,
        ];
    }
}
