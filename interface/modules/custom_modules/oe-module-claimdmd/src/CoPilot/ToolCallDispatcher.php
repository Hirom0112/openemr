<?php

declare(strict_types=1);

namespace OpenEMR\Modules\ClaimDmd\CoPilot;

/**
 * ToolCallDispatcher
 *
 * Dispatches LLM-generated tool calls to the appropriate handler.
 *
 * SECURITY NOTE (VUL-0001):
 *   Panel authorization is the FIRST operation performed for every tool call
 *   that references a patient identifier. No FHIR data is fetched, no briefing
 *   is constructed, and no downstream handler is invoked until
 *   PanelAuthorizationMiddleware::assertPatientInPanel() has returned without
 *   throwing. This cannot be bypassed by conversation-history content.
 */
class ToolCallDispatcher
{
    private PanelAuthorizationMiddleware $panelAuth;
    /** @var array<string, callable> */
    private array $handlers;

    public function __construct(
        PanelAuthorizationMiddleware $panelAuth,
        array $handlers = []
    ) {
        $this->panelAuth = $panelAuth;
        $this->handlers  = $handlers;
    }

    /**
     * Dispatch a tool call.
     *
     * @param string $toolName        Name of the tool to invoke.
     * @param array  $toolArgs        Arguments parsed from the LLM response.
     * @param string $clinicianId     Authenticated clinician ID (from session — NOT from conversation).
     *
     * @return mixed Tool handler return value.
     *
     * @throws PanelAuthorizationException  If the patient is not in the clinician's panel.
     * @throws \InvalidArgumentException    If the tool name is unknown.
     */
    public function dispatch(string $toolName, array $toolArgs, string $clinicianId): mixed
    {
        // ----------------------------------------------------------------
        // SECURITY GATE (VUL-0001): Pre-tool-call panel authorization.
        // Any tool call that references a patient_id MUST pass this check
        // before any handler is invoked. The clinicianId is sourced from
        // the authenticated session — NEVER from conversation history.
        // ----------------------------------------------------------------
        if (isset($toolArgs['patient_id'])) {
            $this->panelAuth->assertPatientInPanel(
                $clinicianId,
                (string) $toolArgs['patient_id']
            );
        }

        if (!isset($this->handlers[$toolName])) {
            throw new \InvalidArgumentException(
                sprintf('Unknown CoPilot tool: %s', $toolName)
            );
        }

        return ($this->handlers[$toolName])($toolArgs, $clinicianId);
    }

    /**
     * Register a tool handler.
     *
     * @param string   $toolName  Tool name as declared in the LLM function schema.
     * @param callable $handler   fn(array $args, string $clinicianId): mixed
     */
    public function registerHandler(string $toolName, callable $handler): void
    {
        $this->handlers[$toolName] = $handler;
    }
}
