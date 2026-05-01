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
    private const MODULE_URL = '/interface/modules/custom_modules/oe-module-clinical-copilot/index.php';
    private const JSON_FLAGS = JSON_HEX_TAG | JSON_HEX_APOS | JSON_HEX_QUOT | JSON_HEX_AMP;

    public function __construct(
        private readonly EventDispatcherInterface $eventDispatcher,
    ) {}

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
        $item->url         = self::MODULE_URL;
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
        $sessionData     = $_SESSION['OpenEMR'] ?? [];
        $providerId      = $sessionData['authUserID'] ?? null;
        $patientIds      = $sessionData['copilot_patient_ids'] ?? [];
        $sessionId       = session_id();
        $agentApiUrl     = getenv('COPILOT_AGENT_API_URL') ?: 'http://localhost:8400';

        $payload = [
            'session_id'  => $sessionId,
            'provider_id' => $providerId,
            'patient_ids' => is_array($patientIds) ? array_values($patientIds) : [],
        ];

        $payloadJson     = json_encode($payload, self::JSON_FLAGS);
        $agentApiUrlJson = json_encode($agentApiUrl, self::JSON_FLAGS);
        $moduleUrlJson   = json_encode(self::MODULE_URL, self::JSON_FLAGS);

        if ($payloadJson === false || $agentApiUrlJson === false || $moduleUrlJson === false) {
            return;
        }

        echo <<<HTML
<script>
(function () {
    try {
        var agentApiUrl = {$agentApiUrlJson};
        var payload = {$payloadJson};
        var moduleUrl = {$moduleUrlJson};

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
                    if (tUrl && String(tUrl).indexOf('cop') !== -1 && String(tUrl).indexOf(moduleUrl) !== -1) {
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
