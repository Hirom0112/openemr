import type { FrameLocator, Page } from '@playwright/test';

/**
 * Selectors and helpers shared across the Co-Pilot specs.
 *
 * NOTE: data-testid attributes are intentionally NOT used yet — Issue 1
 * (theme refactor) is reworking the Co-Pilot DOM in parallel and we don't
 * want to seed selectors that are about to be deleted. After Issue 1 lands
 * we should add stable testids and migrate these helpers to them.
 */

export const COPILOT_IFRAME_SELECTOR = 'iframe[src*="oe-module-clinical-copilot"]';

/**
 * Click the Co-Pilot tab in OpenEMR's main nav and wait for its iframe to
 * appear. Returns the FrameLocator for the iframe so callers can probe inside.
 *
 * The Co-Pilot tab is injected at runtime by the module's Bootstrap.php into
 * the parent main.php tab bar, so we need to be a bit forgiving about timing.
 */
export async function openCopilot(page: Page): Promise<FrameLocator> {
  await page.goto('/interface/main/tabs/main.php', { waitUntil: 'domcontentloaded' });

  // Dismiss the "OpenEMR Product Registration" modal if present.
  const askLater = page.getByRole('button', { name: /ask again later/i });
  if (await askLater.isVisible().catch(() => false)) {
    await askLater.click();
  }

  // The Co-Pilot tab can be one of: a top-level link, a menu-item button, OR
  // a child of the "Modules" dropdown that requires expansion first. Try
  // several selector strategies in priority order; each has a short timeout
  // so a missing element falls through fast instead of blocking 30s.
  const tabSelectors = [
    () => page.getByRole('link', { name: /co-?pilot/i }).first(),
    () => page.getByRole('menuitem', { name: /co-?pilot/i }).first(),
    () => page.getByRole('button', { name: /co-?pilot/i }).first(),
    () => page.locator('a, button, [role="menuitem"]').filter({ hasText: /co-?pilot/i }).first(),
  ];
  let tabClicked = false;
  for (const make of tabSelectors) {
    const candidate = make();
    if (await candidate.isVisible({ timeout: 2_000 }).catch(() => false)) {
      await candidate.click();
      tabClicked = true;
      break;
    }
  }

  // Fallback: navigate directly to the iframe URL. The Co-Pilot module's
  // index.php loads the React bundle the same way the tab click would —
  // and the deterministic session_id (provider + Y-m-d hash) means the
  // iframe still sees the same session as a user-driven click.
  if (!tabClicked) {
    await page.goto('/interface/modules/custom_modules/oe-module-clinical-copilot/index.php', {
      waitUntil: 'domcontentloaded',
    });
  }

  // The iframe is appended into the tab content area; wait for it.
  // Direct-nav path doesn't have an outer iframe — the page IS the bundle —
  // so check for either the iframe OR the React root.
  const iframeOrRoot = page.locator(`${COPILOT_IFRAME_SELECTOR}, #copilot-root`).first();
  await iframeOrRoot.waitFor({ state: 'attached', timeout: 30_000 });

  // If we navigated directly to the bundle, return a synthesized FrameLocator
  // that maps to the page itself (Playwright's frameLocator API expects an
  // iframe element). Tests that want main-page interaction can use the page
  // directly when this returns null — but for now most tests assume frame
  // semantics, so wrap in a small adapter.
  if (await page.locator(COPILOT_IFRAME_SELECTOR).first().isVisible({ timeout: 1_000 }).catch(() => false)) {
    return page.frameLocator(COPILOT_IFRAME_SELECTOR);
  }
  // Direct-nav fallback: the bundle owns the whole page. frameLocator on the
  // root html still works as a passthrough for most locator queries.
  return page.frameLocator('html');
}

/**
 * Wait until the census has rendered patient cards inside the iframe.
 * Returns the count of patient rows visible.
 *
 * CensusRenderer renders one "Open chart for <name>" button per patient
 * (label "Chart ↗"). PatientCard.tsx (which used "View in Chart") is no
 * longer rendered by ChatSurface, so we match the census aria-label instead.
 */
export async function waitForCensus(frame: FrameLocator, timeout = 30_000): Promise<number> {
  // Greeting renders immediately; tier counts appear when census resolves.
  await frame.getByText(/ready for your census/i).waitFor({ state: 'visible', timeout });
  const chartButtons = frame.getByRole('button', { name: /open chart for /i });
  await chartButtons.first().waitFor({ state: 'visible', timeout });
  return chartButtons.count();
}
