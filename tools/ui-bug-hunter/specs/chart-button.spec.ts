import { expect, test } from '@playwright/test';
import { openCopilot, waitForCensus } from './_helpers';

/**
 * Issue: clicking "Chart ↗" on a census patient row must switch OpenEMR's
 * patient frame ("pat" tab) to the demographics view with the correct integer
 * pid in the URL.
 *
 * History: agent-ui/src/components/PatientCard.tsx originally opened a popup
 * via window.open(...). Commit 456709e3f switched the census flow
 * (CensusRenderer.openPatientChart) to in-frame navigation:
 *
 *   w.top.RTop.location = `/interface/patient_file/summary/demographics.php?set_pid=${pid}`;
 *
 * RTop is defined in interface/main/tabs/js/frame_proxies.js and proxies the
 * assignment through navigateTab(url, "pat") — so the URL ends up loaded into
 * the frame named "pat" inside main.php's tab strip, NOT a popup.
 *
 * This spec asserts that clicking the button drives some frame on the page to
 * a demographics URL with a numeric `set_pid` and that the loaded body is
 * demographics-shaped (not an empty / "patient not found" page).
 */
test.describe('Co-Pilot Chart button — in-frame navigation', () => {
  test.use({ storageState: '.auth/sara.json' });

  test('first three census rows route the patient frame to demographics', async ({ page }) => {
    const SAMPLE_SIZE = 3;

    for (let i = 0; i < SAMPLE_SIZE; i++) {
      // Re-open copilot every iteration: clicking Chart activates the patient
      // tab, hiding the copilot pane, so we need a fresh census view.
      const frame = await openCopilot(page);
      const total = await waitForCensus(frame);
      expect(total).toBeGreaterThan(i);

      const chartButton = frame.getByRole('button', { name: /open chart for /i }).nth(i);
      await chartButton.click();

      // Poll all (same-origin) frames in the top window for one whose URL
      // matches demographics with a numeric set_pid. RTop.location = url
      // delegates to navigateTab(url, "pat") which loads the URL into the
      // iframe named "pat" — there is no popup event to await.
      const handle = await page.waitForFunction(
        () => {
          const all: Window[] = [];
          const walk = (w: Window) => {
            try {
              all.push(w);
              for (let j = 0; j < w.frames.length; j++) walk(w.frames[j]);
            } catch {
              /* cross-origin: skip */
            }
          };
          walk(window.top!);
          for (const w of all) {
            try {
              const href = w.location.href;
              if (/\/interface\/patient_file\/summary\/demographics(_full)?\.php\?.*set_pid=\d+/.test(href)) {
                return href;
              }
            } catch {
              /* ignore */
            }
          }
          // Fallback: scan iframe element src attributes.
          const iframes = window.top!.document.querySelectorAll('iframe');
          for (const el of Array.from(iframes)) {
            const src = (el as HTMLIFrameElement).src;
            if (/\/interface\/patient_file\/summary\/demographics(_full)?\.php\?.*set_pid=\d+/.test(src)) {
              return src;
            }
          }
          return null;
        },
        null,
        { timeout: 30_000 },
      );

      const url = (await handle.jsonValue()) as string;
      expect(url, `chart URL for row ${i}`).toMatch(/set_pid=\d+(&|$)/);

      // Find the Playwright Frame matching that URL and probe its body.
      let demoFrame = page
        .frames()
        .find((f) => /demographics(_full)?\.php\?.*set_pid=\d+/.test(f.url()));
      if (!demoFrame) {
        await page.waitForTimeout(500);
        demoFrame = page
          .frames()
          .find((f) => /demographics(_full)?\.php\?.*set_pid=\d+/.test(f.url()));
      }
      expect(demoFrame, `demographics frame for row ${i}`).toBeDefined();
      await demoFrame!.waitForLoadState('domcontentloaded');

      const body = (await demoFrame!.locator('body').textContent()) ?? '';
      expect(body, `demographics body for row ${i}`).not.toMatch(/no\s+records?\s+found/i);
      expect(body, `demographics body for row ${i}`).not.toMatch(/patient\s+not\s+found/i);
      expect(body.length, `demographics body for row ${i}`).toBeGreaterThan(200);
    }
  });
});
