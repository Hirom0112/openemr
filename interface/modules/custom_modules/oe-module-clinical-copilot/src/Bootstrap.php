<?php

/**
 * Clinical Co-Pilot — Module Bootstrap
 *
 * Adds a "Co-Pilot" top-level navigation tab via MenuEvent::MENU_UPDATE.
 * The tab opens index.php in its own content iframe, exactly like Calendar
 * or Messages — no overlay, no fixed positioning, no layout side-effects.
 *
 * Also subscribes to RenderEvent::EVENT_BODY_RENDER_PRE to (1) fire a
 * fire-and-forget prefetch to the agent API to warm caches at login and
 * (2) push the Co-Pilot tab into the OpenEMR tab bar in the background so
 * the iframe pre-loads while the user works in another tab.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Clinical Co-Pilot Contributors
 * @copyright Copyright (c) 2024 Clinical Co-Pilot Contributors
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCopilot;

use OpenEMR\Events\Main\Tabs\RenderEvent;
use OpenEMR\Menu\MenuEvent;
use Symfony\Contracts\EventDispatcher\EventDispatcherInterface;

class Bootstrap
{
    private const MODULE_PATH = '/interface/modules/custom_modules/oe-module-clinical-copilot/index.php';
    private const JSON_FLAGS = JSON_HEX_TAG | JSON_HEX_APOS | JSON_HEX_QUOT | JSON_HEX_AMP;

    public function __construct(
        private readonly EventDispatcherInterface $eventDispatcher,
    ) {}

    /**
     * Module URL with a cache-buster derived from index.php's mtime.
     *
     * The Co-Pilot iframe is pushed onto the tab bar via JavaScript at runtime,
     * so a browser hard-reload of the parent page does NOT bypass the iframe's
     * subresource cache. Without a versioned URL, edits to index.php (e.g. the
     * tab-title bootstrap script) can be served from cache indefinitely.
     */
    private function moduleUrl(): string
    {
        $indexFile = __DIR__ . '/../index.php';
        $version   = @filemtime($indexFile) ?: time();
        return self::MODULE_PATH . '?v=' . $version;
    }

    public function subscribeToEvents(): void
    {
        $this->eventDispatcher->addListener(
            MenuEvent::MENU_UPDATE,
            $this->addCopilotMenuItem(...)
        );

        $this->eventDispatcher->addListener(
            RenderEvent::EVENT_BODY_RENDER_PRE,
            $this->injectCopilotBootScript(...)
        );
    }

    public function addCopilotMenuItem(MenuEvent $event): void
    {
        $menu = $event->getMenu();

        $item = new \stdClass();
        $item->label       = xlt('Co-Pilot');
        $item->menu_id     = 'cop0';
        $item->target      = 'cop';
        $item->url         = $this->moduleUrl();
        $item->children    = [];
        $item->requirement = 0;

        $menu[] = $item;
        $event->setMenu($menu);
    }

    /**
     * Inject an inline boot script into the main tabs <body>.
     *
     * Runs once per page-load after login. Performs two background tasks:
     *   1. Fire-and-forget POST to the agent API /agent/prefetch endpoint to
     *      warm caches before the user clicks the Co-Pilot tab.
     *   2. Push the Co-Pilot tab into the Knockout-driven tab bar so its
     *      iframe pre-loads in the background (visible:false → no focus
     *      steal from Calendar/Patient).
     */
    public function injectCopilotBootScript(RenderEvent $event): void
    {
        // Read from both nested and top-level $_SESSION so the module works
        // against the local dev branch (HttpSessionFactory nests under
        // $_SESSION['OpenEMR']) and the published Docker image (top-level).
        $sessionData     = $_SESSION['OpenEMR'] ?? [];
        $providerId      = $sessionData['authUserID']         ?? $_SESSION['authUserID']         ?? null;
        $patientIds      = $sessionData['copilot_patient_ids'] ?? $_SESSION['copilot_patient_ids'] ?? [];
        $sessionId       = session_id();

        // Resolve the agent API URL the same way index.php does. When neither
        // COPILOT_AGENT_API_URL nor a clearly-local context is present, skip
        // the prefetch entirely — fire-and-forget POSTs to localhost from a
        // deployed container only burn time and pollute logs.
        $envAgentApiUrl = getenv('COPILOT_AGENT_API_URL');
        $httpHost       = (string) ($_SERVER['HTTP_HOST'] ?? '');
        $isLocalRequest = $httpHost === ''
            || str_contains($httpHost, 'localhost')
            || str_contains($httpHost, '127.0.0.1');
        $devMode        = getenv('COPILOT_DEV_MODE') === '1';
        if (is_string($envAgentApiUrl) && $envAgentApiUrl !== '') {
            $agentApiUrl = $envAgentApiUrl;
        } elseif ($isLocalRequest || $devMode) {
            $agentApiUrl = 'http://localhost:8400';
        } else {
            error_log('[clinical-copilot] skipping agent prefetch: COPILOT_AGENT_API_URL not configured');
            return;
        }

        $payload = [
            'session_id'  => $sessionId,
            'provider_id' => $providerId,
            'patient_ids' => is_array($patientIds) ? array_values($patientIds) : [],
        ];

        $payloadJson     = json_encode($payload, self::JSON_FLAGS);
        $agentApiUrlJson = json_encode($agentApiUrl, self::JSON_FLAGS);
        $moduleUrlJson   = json_encode($this->moduleUrl(), self::JSON_FLAGS);
        $modulePathJson  = json_encode(self::MODULE_PATH, self::JSON_FLAGS);

        if ($payloadJson === false || $agentApiUrlJson === false || $moduleUrlJson === false || $modulePathJson === false) {
            return;
        }

        echo <<<HTML
<script>
(function () {
    try {
        var agentApiUrl = {$agentApiUrlJson};
        var payload = {$payloadJson};
        var moduleUrl = {$moduleUrlJson};
        var modulePath = {$modulePathJson};

        // Fix 1: warm agent caches in the background.
        try {
            fetch(agentApiUrl + '/agent/prefetch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
                keepalive: true
            }).catch(function () {});
        } catch (e) { /* silent */ }

        // Fix 2: pre-load the Co-Pilot iframe in the tab bar (hidden).
        function pushCopilotTab() {
            try {
                if (typeof app_view_model === 'undefined' || !app_view_model) { return false; }
                var appData = app_view_model.application_data;
                if (!appData || !appData.tabs || typeof appData.tabs.tabsList !== 'function') { return false; }
                if (typeof tabStatus !== 'function') { return false; }

                var existing = appData.tabs.tabsList();
                for (var i = 0; i < existing.length; i++) {
                    var t = existing[i];
                    var tUrl = (t && typeof t.url === 'function') ? t.url() : (t && t.url);
                    // Match by path so a bumped cache-buster does not produce a duplicate.
                    if (tUrl && String(tUrl).indexOf(modulePath) !== -1) {
                        return true;
                    }
                }

                appData.tabs.tabsList.push(new tabStatus(
                    'Co-Pilot',
                    moduleUrl,
                    'cop',
                    'Loading Co-Pilot',
                    true,
                    false,
                    false
                ));
                return true;
            } catch (e) {
                return false;
            }
        }

        if (document.readyState === 'complete' || document.readyState === 'interactive') {
            if (!pushCopilotTab()) {
                var attempts = 0;
                var iv = setInterval(function () {
                    attempts++;
                    if (pushCopilotTab() || attempts > 40) { clearInterval(iv); }
                }, 250);
            }
        } else {
            document.addEventListener('DOMContentLoaded', function () {
                if (!pushCopilotTab()) {
                    var attempts = 0;
                    var iv = setInterval(function () {
                        attempts++;
                        if (pushCopilotTab() || attempts > 40) { clearInterval(iv); }
                    }, 250);
                }
            });
        }

        // Listen for chart-open requests posted from the Co-Pilot iframe.
        // The iframe cannot reliably access top.tabStatus directly, so it
        // sends a postMessage and this handler — running in the main frame —
        // opens the patient chart as a proper OpenEMR tab.
        window.addEventListener('message', function (evt) {
            try {
                if (!evt.data || evt.data.type !== 'copilot:openChart') { return; }
                var chartUrl = evt.data.url;
                if (!chartUrl || typeof chartUrl !== 'string') { return; }
                if (typeof restoreSession === 'function') { restoreSession(); }
                if (typeof app_view_model !== 'undefined' && typeof tabStatus === 'function') {
                    app_view_model.application_data.tabs.tabsList.push(
                        new tabStatus('Patient Chart', chartUrl, 'pat', 'Loading chart…', true, true, false)
                    );
                }
            } catch (e) { /* silent */ }
        });
    } catch (e) { /* silent */ }
})();
</script>
HTML;
    }
}
