import { expect, test } from '@playwright/test';
import { openCopilot, waitForCensus } from './_helpers';

/**
 * Issue 2: census output should be deterministic — reloading the Co-Pilot
 * panel should not change the count of patients in the rendered list.
 *
 * Currently SKIPPED via test.fixme() because Issue 2 is not yet fixed; the
 * spec is authored now so it can be flipped on the moment the fix lands.
 */
test.describe('Co-Pilot census stability across reloads', () => {
  test.use({ storageState: '.auth/sara.json' });

  test.fixme(
    'patient count is stable over 10 reloads (enable once Issue 2 is fixed)',
    async ({ page }) => {
      const RELOADS = 10;
      const counts: number[] = [];

      for (let i = 0; i < RELOADS; i++) {
        const frame = await openCopilot(page);
        const count = await waitForCensus(frame);
        counts.push(count);
        // Force a fresh fetch on the next iteration.
        await page.reload({ waitUntil: 'domcontentloaded' });
      }

      // Attach counts so failures are diagnosable from the HTML report.
      test.info().annotations.push({
        type: 'census-counts',
        description: counts.join(','),
      });

      expect(new Set(counts).size).toBe(1);
    },
  );
});
