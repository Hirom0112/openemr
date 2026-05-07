<?php

/**
 * Patient Dashboard Port — Module Bootstrap
 *
 * Adds a "Dashboard (Port)" top-level navigation tab via
 * MenuEvent::MENU_UPDATE. The tab opens index.php in its own content
 * iframe, exactly like Calendar or Messages — no overlay, no fixed
 * positioning, no layout side-effects.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Patient Dashboard Port Contributors
 * @copyright Copyright (c) 2026 Patient Dashboard Port Contributors
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\PatientDashboardPort;

use OpenEMR\Menu\MenuEvent;
use Symfony\Contracts\EventDispatcher\EventDispatcherInterface;

class Bootstrap
{
    private const MODULE_PATH = '/interface/modules/custom_modules/oe-module-patient-dashboard-port/index.php';

    public function __construct(
        private readonly EventDispatcherInterface $eventDispatcher,
    ) {
    }

    /**
     * Module URL with a cache-buster derived from index.php's mtime.
     * Without this a hard-reload of the parent shell still serves the
     * iframe's previously-cached resources.
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
            $this->addDashboardMenuItem(...)
        );
    }

    public function addDashboardMenuItem(MenuEvent $event): void
    {
        $menu = $event->getMenu();

        $item              = new \stdClass();
        $item->label       = xlt('Dashboard (Modern)');
        $item->menu_id     = 'pdp0';
        $item->target      = 'pdp';
        $item->url         = $this->moduleUrl();
        $item->children    = [];
        $item->requirement = 0;

        $menu[] = $item;
        $event->setMenu($menu);
    }
}
