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

  // The tab is a normal text link with the visible label "Co-Pilot".
  const tab = page.getByRole('link', { name: /co-?pilot/i }).first();
  await tab.waitFor({ state: 'visible', timeout: 30_000 });
  await tab.click();

  // The iframe is appended into the tab content area; wait for it.
  await page.locator(COPILOT_IFRAME_SELECTOR).first().waitFor({ state: 'attached', timeout: 30_000 });

  return page.frameLocator(COPILOT_IFRAME_SELECTOR);
}

/**
 * Wait until the census has rendered patient cards inside the iframe.
 * Returns the count of P-cards visible.
 */
export async function waitForCensus(frame: FrameLocator, timeout = 30_000): Promise<number> {
  // Greeting renders immediately; tier counts appear when census resolves.
  await frame.getByText(/ready for your census/i).waitFor({ state: 'visible', timeout });
  // Cards have a "View in Chart" button — wait for at least one.
  await frame.getByRole('button', { name: /view in chart/i }).first().waitFor({ state: 'visible', timeout });
  return frame.getByRole('button', { name: /view in chart/i }).count();
}
