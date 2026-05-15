<?php

declare(strict_types=1);

namespace OpenEMR\Modules\ClaimDmd\CoPilot;

/**
 * ConversationHandler
 *
 * Manages multi-turn conversation state for the CoPilot feature.
 *
 * SECURITY NOTE (VUL-0001):
 *   Conversation history is UNTRUSTED USER INPUT. Prior turns may contain
 *   social-engineering assertions such as cross-coverage claims
 *   ("I cover for another hospitalist on weekends"). These assertions MUST
 *   NOT influence authorization decisions. This class never elevates
 *   permissions based on conversation content and never passes authorization-
 *   relevant claims to downstream components.
 *
 *   Authorization is always decided by PanelAuthorizationMiddleware using
 *   the authoritative panel store, regardless of what the conversation
 *   history contains.
 */
class ConversationHandler
{
    /** @var list<array{role: string, content: string}> */
    private array $history = [];

    private ToolCallDispatcher $dispatcher;
    private string $authenticatedClinicianId;

    /**
     * Patterns that represent attempted authorization bypass via conversation.
     * Matching turns are recorded for audit purposes but NEVER acted upon.
     *
     * @var list<string>
     */
    private const CROSS_COVERAGE_BYPASS_PATTERNS = [
        '/cross.?cover/i',
        '/covering for another/i',
        '/not in my usual panel/i',
        '/may not be in my.*panel/i',
        '/on behalf of/i',
        '/acting as/i',
    ];

    public function __construct(
        ToolCallDispatcher $dispatcher,
        string $authenticatedClinicianId
    ) {
        $this->dispatcher               = $dispatcher;
        // Clinician ID is set once at construction from the authenticated
        // session. It cannot be changed by conversation input.
        $this->authenticatedClinicianId = $authenticatedClinicianId;
    }

    /**
     * Append a user turn to the conversation and return the assistant reply.
     *
     * The conversation history passed to the LLM NEVER includes authorization-
     * relevant metadata derived from prior turns.
     *
     * @param string $userMessage Raw user message text.
     * @param callable $llmCallback fn(array $messages): array{content: string, tool_calls: array}
     *
     * @return string Assistant response text (safe to return to the caller).
     */
    public function handleTurn(string $userMessage, callable $llmCallback): string
    {
        // Log potential bypass attempts for audit, but take no other action.
        $this->auditIfBypassAttempt($userMessage);

        // Append the user message to history as-is (raw input, untrusted).
        $this->history[] = ['role' => 'user', 'content' => $userMessage];

        // Build the message array for the LLM. History is forwarded for
        // conversational context only — it carries no authorization weight.
        $messages = $this->buildMessagesForLlm();

        $llmResponse = $llmCallback($messages);

        // Handle tool calls if the LLM requested any.
        $assistantContent = $this->resolveToolCalls($llmResponse);

        $this->history[] = ['role' => 'assistant', 'content' => $assistantContent];

        return $assistantContent;
    }

    /**
     * Resolve tool calls from an LLM response.
     *
     * Authorization is enforced by ToolCallDispatcher (which calls
     * PanelAuthorizationMiddleware) before any handler executes.
     * PanelAuthorizationException is caught here and converted to a safe
     * user-facing message — no patient data leaks on denial.
     *
     * @param array $llmResponse
     * @return string
     */
    private function resolveToolCalls(array $llmResponse): string
    {
        if (empty($llmResponse['tool_calls'])) {
            return $llmResponse['content'] ?? '';
        }

        $resultParts = [];

        foreach ($llmResponse['tool_calls'] as $toolCall) {
            $toolName = $toolCall['function']['name'] ?? '';
            $toolArgs = $toolCall['function']['arguments'] ?? [];

            if (is_string($toolArgs)) {
                $toolArgs = json_decode($toolArgs, true) ?? [];
            }

            try {
                // Panel authorization gate is inside dispatch().
                // clinicianId comes from the authenticated session — NOT conversation.
                $result = $this->dispatcher->dispatch(
                    $toolName,
                    $toolArgs,
                    $this->authenticatedClinicianId
                );
                $resultParts[] = is_string($result) ? $result : json_encode($result);
            } catch (PanelAuthorizationException) {
                // Return a safe, non-leaking denial message.
                // Do NOT include the patient ID or any clinical data in this string.
                $resultParts[] = 'I\'m sorry, I\'m unable to retrieve information for that patient. '
                    . 'The patient is not in your assigned panel. '
                    . 'If you believe this is an error, please contact your administrator.';
            }
        }

        return implode("\n", $resultParts);
    }

    /**
     * Build the message array to send to the LLM.
     *
     * Returns a copy of history. No authorization claims are injected.
     *
     * @return list<array{role: string, content: string}>
     */
    private function buildMessagesForLlm(): array
    {
        return $this->history;
    }

    /**
     * Detect and log potential cross-coverage bypass attempts in user input.
     * Detection is for audit/alerting only — it does not grant or deny access.
     */
    private function auditIfBypassAttempt(string $userMessage): void
    {
        foreach (self::CROSS_COVERAGE_BYPASS_PATTERNS as $pattern) {
            if (preg_match($pattern, $userMessage) === 1) {
                error_log(sprintf(
                    '[CoPilot][SECURITY] Potential cross-coverage bypass pattern detected '
                    . 'in conversation turn. clinician_id=%s pattern=%s',
                    $this->authenticatedClinicianId,
                    $pattern
                ));
                // Continue — detection does not halt conversation flow.
                // Authorization is enforced at tool-call time regardless.
                break;
            }
        }
    }

    /**
     * Return a copy of the current conversation history (read-only view).
     *
     * @return list<array{role: string, content: string}>
     */
    public function getHistory(): array
    {
        return $this->history;
    }

    /**
     * Clear conversation history (e.g., on session end).
     */
    public function clearHistory(): void
    {
        $this->history = [];
    }
}
