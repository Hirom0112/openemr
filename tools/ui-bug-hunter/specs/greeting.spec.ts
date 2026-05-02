import { expect, test } from '@playwright/test';
import { openCopilot } from './_helpers';

/**
 * Issue: ChatSurface greeting must interpolate the logged-in clinician name.
 * Reference: agent-ui/src/components/ChatSurface.tsx — `Good day, ${displayName} — ready for your census`.
 *
 * Regression we're guarding against: the placeholder "{User}" leaking into
 * the rendered greeting (caught in earlier hunts as a templating bug).
 */
test.describe('Co-Pilot greeting', () => {
  test.use({ storageState: '.auth/sara.json' });

  test('renders personalized greeting without placeholder leak', async ({ page }) => {
    const frame = await openCopilot(page);

    // The greeting is rendered as a plain text node by the ChatSurface system message.
    const greeting = frame.getByText(/good day,\s+\S+.*?ready for your census/i).first();
    await expect(greeting).toBeVisible({ timeout: 30_000 });

    const text = (await greeting.textContent()) ?? '';
    expect(text).toMatch(/^Good day,\s+\S+.*?ready for your census/i);
    expect(text).not.toContain('{User}');
    expect(text).not.toContain('{user}');
    expect(text).not.toMatch(/undefined|null/i);
  });
});
