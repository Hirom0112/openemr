import { expect, test } from '@playwright/test';
import { openCopilot, waitForCensus } from './_helpers';

/**
 * Issue 1: every tool-output container (the "assistant bubble" that wraps
 * each renderer's output) should share canonical theme tokens — same
 * background, border, border-radius, and font-family.
 *
 * The original spec waited on `[role="article"], .tool-output, [data-tool-output]`
 * — none of those exist in the React bundle. The actual canonical container
 * is `styles.assistantBubble` in agent-ui/src/components/ChatSurface.tsx
 * (line ~722), which is rendered as an inline-styled <div> wrapping every
 * assistant ResponseRenderer. Its fingerprint is the unique
 * `border-radius: 4px 14px 14px 14px` chat-bubble shape.
 *
 * We trigger a second tool output (a briefing) so that the message stream
 * contains at least two assistant bubbles (initial census + briefing), then
 * assert that all bubbles share the same computed background, border-radius,
 * and font-family.
 */
test.describe('Co-Pilot tool-output visual consistency', () => {
  test.use({ storageState: '.auth/sara.json' });

  test('assistant bubbles share canonical theme tokens', async ({ page }) => {
    const frame = await openCopilot(page);
    await waitForCensus(frame);

    // Trigger a briefing so the message list has more than one assistant
    // bubble in the DOM at the same time.
    const input = frame.getByPlaceholder(/Ask about a patient/i);
    await input.fill('Brief Marcus Webb');
    await input.press('Enter');
    // Wait for an assistant response with a heading inside (briefing renders
    // SectionHeading h2 elements).
    await frame.locator('h1, h2, h3').nth(1).waitFor({ state: 'visible', timeout: 60_000 });

    // The assistantBubble fingerprint: inline style with the bubble-shaped
    // border-radius. Match on the style attribute substring rather than a
    // CSS class (the bundle does not use classes for these containers).
    const styles = await frame
      .locator('div[style*="border-radius: 4px 14px 14px 14px"]')
      .evaluateAll((nodes) =>
        nodes.map((n) => {
          const cs = window.getComputedStyle(n as HTMLElement);
          return {
            backgroundColor: cs.backgroundColor,
            borderRadius: cs.borderRadius,
            fontFamily: cs.fontFamily,
            borderColor: cs.borderColor,
          };
        }),
      );

    expect(styles.length, 'expected at least 2 assistant bubbles in the stream').toBeGreaterThan(1);

    const distinctBg = new Set(styles.map((s) => s.backgroundColor));
    const distinctRadius = new Set(styles.map((s) => s.borderRadius));
    const distinctFont = new Set(styles.map((s) => s.fontFamily));
    const distinctBorder = new Set(styles.map((s) => s.borderColor));

    expect(distinctBg.size, `backgrounds: ${[...distinctBg].join(' | ')}`).toBe(1);
    expect(distinctRadius.size, `radii: ${[...distinctRadius].join(' | ')}`).toBe(1);
    expect(distinctFont.size, `fonts: ${[...distinctFont].join(' | ')}`).toBe(1);
    expect(distinctBorder.size, `borders: ${[...distinctBorder].join(' | ')}`).toBe(1);
  });
});
