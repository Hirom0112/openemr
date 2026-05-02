import { expect, test } from '@playwright/test';
import { openCopilot, waitForCensus } from './_helpers';

/**
 * Issue: clicking "View in Chart" on a patient card must open OpenEMR's
 * demographics view in a popup with the correct integer pid in the URL.
 *
 * The button (agent-ui/src/components/PatientCard.tsx) calls
 *   window.open(`${origin}/interface/patient_file/summary/demographics_full.php?set_pid=${patient.patient_id}`, '_blank');
 * — so we hook page.waitForEvent('popup') and assert URL + body content.
 */
test.describe('Co-Pilot chart-button popups', () => {
  test.use({ storageState: '.auth/sara.json' });

  test('first three patient cards open demographics for a real pid', async ({ page }) => {
    const frame = await openCopilot(page);
    await waitForCensus(frame);

    const chartButtons = frame.getByRole('button', { name: /view in chart/i });
    const totalButtons = await chartButtons.count();
    const sampleSize = Math.min(3, totalButtons);
    expect(sampleSize).toBeGreaterThan(0);

    for (let i = 0; i < sampleSize; i++) {
      const [popup] = await Promise.all([
        page.context().waitForEvent('page'),
        chartButtons.nth(i).click(),
      ]);

      await popup.waitForLoadState('domcontentloaded');
      const url = popup.url();
      expect(url, `popup URL for card ${i}`).toMatch(/set_pid=\d+(&|$)/);

      // Demographics body should be populated — at minimum the page should
      // not be displaying a "no records" / "patient not found" banner.
      const body = (await popup.locator('body').textContent()) ?? '';
      expect(body, `popup body for card ${i}`).not.toMatch(/no\s+records?\s+found/i);
      expect(body, `popup body for card ${i}`).not.toMatch(/patient\s+not\s+found/i);
      // Demographics page typically shows "Demographics" or a name header.
      expect(body.length, `popup body for card ${i}`).toBeGreaterThan(200);

      await popup.close();
    }
  });
});
