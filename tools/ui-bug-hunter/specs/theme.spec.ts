import { expect, test } from '@playwright/test';
import { openCopilot, waitForCensus } from './_helpers';

/**
 * Issue 1: every tool-output container in the Co-Pilot panel should render
 * with the same canonical visual tokens (background, border-radius, heading
 * font/color). Today, briefing/query/handoff renderers each apply their own
 * styles inline; this drifts.
 *
 * SKIPPED via test.fixme() until Issue 1 (theme refactor) lands. The spec is
 * here so it can flip on with one keyword change.
 */
test.describe('Co-Pilot tool-output visual consistency', () => {
  test.use({ storageState: '.auth/sara.json' });

  test.fixme(
    'tool-output containers share canonical theme tokens (enable once Issue 1 is fixed)',
    async ({ page }) => {
      const frame = await openCopilot(page);
      await waitForCensus(frame);

      // Trigger a second tool output (briefing) so we have at least two
      // distinct renderers in the message stream.
      const input = frame.getByPlaceholder(/Ask about a patient/i);
      await input.fill('Brief Marcus Webb');
      await input.press('Enter');
      // Wait for an assistant response with a heading inside.
      await frame.locator('h1, h2, h3').nth(1).waitFor({ state: 'visible', timeout: 45_000 });

      // Every assistant message wraps its renderer in a container. We sniff
      // common style props across all of them and assert convergence.
      const styles = await frame.locator('[role="article"], .tool-output, [data-tool-output]').evaluateAll(
        (nodes) =>
          nodes.map((n) => {
            const cs = window.getComputedStyle(n as HTMLElement);
            return {
              backgroundColor: cs.backgroundColor,
              borderRadius: cs.borderRadius,
              fontFamily: cs.fontFamily,
            };
          }),
      );

      expect(styles.length).toBeGreaterThan(1);
      const distinctBg = new Set(styles.map((s) => s.backgroundColor));
      const distinctRadius = new Set(styles.map((s) => s.borderRadius));
      const distinctFont = new Set(styles.map((s) => s.fontFamily));

      expect(distinctBg.size).toBeLessThanOrEqual(2);
      expect(distinctRadius.size).toBeLessThanOrEqual(2);
      expect(distinctFont.size).toBeLessThanOrEqual(1);
    },
  );
});
