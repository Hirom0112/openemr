# oe-module-clinical-copilot

Thin OpenEMR custom module that authenticates the session and serves the
Clinical Co-Pilot React panel.

## What this module does

1. Validates the OpenEMR session (`AclMain::aclCheckCore('patients', 'med')`).
2. Injects runtime config into `window.__COPILOT_CONFIG__`.
3. Serves `public/copilot.js` (compiled React bundle).

## Config keys injected by `index.php`

| Key | Source | Purpose |
|---|---|---|
| `agentApiUrl` | `$GLOBALS['copilot_agent_api_url']` (default `http://localhost:8400`) | Base URL for agent-api fetch calls |
| `providerId` | `$_SESSION['authUserID']` | Sent as `provider_id` in every agent-api request |
| `providerName` | `$_SESSION['authUser']` | Displayed in greeting and sent to dispatcher |
| `csrfToken` | `CsrfUtils::collectCsrfToken()` | Available to the React layer for OpenEMR API calls |
| `sessionId` | `session_id()` | Ties agent checkpointer session to the OpenEMR PHP session |
| `patientIds` | `$_SESSION['copilot_patient_ids']` (default `[]`) | Census patient list; React panel reloads via agent-api if empty |

## What this module deliberately does NOT do

- No request proxying — the React panel calls agent-api directly
- No response transformation — agent-api responses go straight to the browser
- No persistent state — no writes between requests
- No session token storage — CSRF token is injected once per page load only

## Bundle

`public/copilot.js` is built from `agent-ui/` with `npm run build`.
Rebuild after any UI changes: `cd agent-ui && npm run build`.
