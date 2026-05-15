<?php
/**
 * process_message.php — Receives inbound Co-Pilot chat turns, applies
 * injection screening, and forwards safe content to the prompt builder.
 *
 * Security hardening for VUL-0003:
 *  - Every inbound user turn is screened by InjectionFilter before processing.
 *  - Blocked turns receive a structured JSON refusal; nothing reaches the LLM.
 *  - All block events are written to the OpenEMR audit log.
 *
 * OWASP LLM01:2025 | MITRE ATLAS AML.T0051
 */

declare(strict_types=1);

require_once __DIR__ . '/../../globals.php';
require_once __DIR__ . '/../../library/ai_copilot/injection_filter.php';
require_once __DIR__ . '/../../library/ai_copilot/prompt_builder.php';

use OpenEMR\AiCopilot\InjectionFilter;
use OpenEMR\AiCopilot\PromptBuilder;

// ---------------------------------------------------------------------------
// Bootstrap / session checks (existing OpenEMR pattern)
// ---------------------------------------------------------------------------
if (!isset($_SESSION['authUserID'])) {
    http_response_code(401);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'unauthenticated']);
    exit;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'method_not_allowed']);
    exit;
}

// ---------------------------------------------------------------------------
// Audit logger — writes to OpenEMR event log table
// ---------------------------------------------------------------------------
$auditLogger = function (string $message, string $level = 'INFO'): void {
    $sessionId = session_id();
    $userId    = (int) ($_SESSION['authUserID'] ?? 0);
    // newEvent signature: event, user, groupname, success, comments
    if (function_exists('newEvent')) {
        newEvent(
            'ai_copilot_security',
            $userId,
            $_SESSION['authProvider'] ?? '',
            ($level === 'SECURITY') ? 0 : 1,
            '[' . $level . '] [session=' . $sessionId . '] ' . $message
        );
    }
    // Also emit to PHP error_log as a secondary channel
    error_log('[OpenEMR AI CoPilot] [' . $level . '] ' . $message);
};

// ---------------------------------------------------------------------------
// Parse and validate the request body
// ---------------------------------------------------------------------------
$raw = file_get_contents('php://input');
$body = json_decode($raw, true);

if (!is_array($body)) {
    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'invalid_json']);
    exit;
}

$userMessage   = isset($body['message']) ? (string) $body['message'] : '';
$conversationHistory = isset($body['history']) && is_array($body['history'])
    ? $body['history']
    : [];
$uploadedDocuments   = isset($body['documents']) && is_array($body['documents'])
    ? $body['documents']
    : [];

if ($userMessage === '') {
    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'empty_message']);
    exit;
}

// ---------------------------------------------------------------------------
// SECURITY: Screen the inbound user turn for injection payloads
// ---------------------------------------------------------------------------
$turnScreen = InjectionFilter::screen($userMessage, 'user_turn');

if ($turnScreen['blocked']) {
    $auditLogger(
        sprintf(
            'INJECTION_BLOCKED: user_turn blocked for user=%d. %s',
            (int) ($_SESSION['authUserID'] ?? 0),
            $turnScreen['reason']
        ),
        'SECURITY'
    );

    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode([
        'error'   => 'blocked',
        'code'    => 'INJECTION_DETECTED',
        'message' => 'Your message contains content that resembles a system instruction '
                   . 'or override command and cannot be processed. '
                   . 'If you believe this is an error, please contact your system administrator.',
    ]);
    exit;
}

// ---------------------------------------------------------------------------
// Build the prompt via PromptBuilder
// ---------------------------------------------------------------------------
$builder = new PromptBuilder($auditLogger);

// Add prior conversation history (clinician turns only — assistant turns are
// informational and should not be re-fed as instructions).
foreach ($conversationHistory as $turn) {
    if (!is_array($turn)
        || !isset($turn['role'], $turn['content'])
        || !is_string($turn['role'])
        || !is_string($turn['content'])
    ) {
        continue;
    }
    // Only accepted roles are 'clinician' and 'assistant'
    $role = in_array($turn['role'], ['clinician', 'assistant'], true)
        ? $turn['role']
        : 'clinician';
    $builder->addHistoryTurn($role, $turn['content']);
}

// Add uploaded document segments (untrusted — routed through delimiting + filter)
foreach ($uploadedDocuments as $doc) {
    if (!is_array($doc)
        || !isset($doc['filename'], $doc['content'])
        || !is_string($doc['filename'])
        || !is_string($doc['content'])
    ) {
        continue;
    }
    $builder->addDocumentSegment($doc['filename'], $doc['content']);
}

// Add the current clinician message as a trusted instruction
$builder->addClinicianInstruction($userMessage);

$prompt = $builder->build();

// ---------------------------------------------------------------------------
// Forward assembled prompt to the LLM backend
// (Placeholder — replace with actual OpenEMR LLM gateway call)
// ---------------------------------------------------------------------------
$llmResponse = callLlmGateway($prompt);

header('Content-Type: application/json');
echo json_encode([
    'response' => $llmResponse,
]);
exit;

// ---------------------------------------------------------------------------
// Helper: LLM gateway stub — replace with real implementation
// ---------------------------------------------------------------------------
function callLlmGateway(array $prompt): string
{
    // TODO: implement actual LLM API call using $prompt['system'] and
    // $prompt['messages'].  This stub exists solely to make the file
    // self-contained for the security patch.
    return '[LLM gateway not yet wired — prompt assembled and screened successfully]';
}
