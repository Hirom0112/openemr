import { expect, test, type FrameLocator, type Page } from '@playwright/test';
import { openCopilot, waitForCensus } from './_helpers';

/**
 * Chat-surface behaviors that the prompt-eval suite cannot exercise:
 *
 *   1. Chart button gating  — only briefings + narrative-flagged answers get
 *      the "Verify in Chart ↗" affordance.
 *   2. Auto-collapse cascade — every fresh assistant bubble collapses prior
 *      assistant bubbles, EXCEPT the initial census (it is the persistent
 *      reference frame; only manual toggle folds it).
 *   3. Census Refresh — clicking Refresh updates the "Census as of HH:MM"
 *      timestamp.
 *   4. Handoff cascade stagger — patient cards expand top-down with a
 *      perceptible gap (EXPAND_STAGGER_MS = 400ms in HandoffRenderer).
 *   5. Markdown rendering — narrative output renders <strong>/<ul> instead
 *      of leaking raw `**` / `##` to the user.
 *
 * All five tests share a single sara session (the clinician persona) and run
 * end-to-end against the live agent-api + OpenEMR — no mocks.
 */

const ASSISTANT_BUBBLE_SELECTOR = 'div[style*="border-radius: 4px 14px 14px 14px"]';
const COLLAPSED_HEADER_RE = /^Expand .* message$/;
const EXPANDED_HEADER_RE = /^Collapse .* message$/;

/**
 * Type a message in the input, hit Enter, then wait until the assistant
 * bubble count grows past `priorBubbleCount`.
 *
 * Returns the new bubble count after the response settles.
 */
async function sendAndAwaitAssistant(
  frame: FrameLocator,
  text: string,
  priorBubbleCount: number,
  timeoutMs = 60_000,
): Promise<number> {
  const input = frame.getByPlaceholder(/Ask about a patient/i);
  await input.fill(text);
  await input.press('Enter');

  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const count = await frame.locator(ASSISTANT_BUBBLE_SELECTOR).count();
    if (count > priorBubbleCount) {
      // Give the response a beat to settle (auto-collapse effect runs after paint).
      await frame.locator(ASSISTANT_BUBBLE_SELECTOR).first().page().waitForTimeout(800);
      return count;
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`assistant bubble never appeared for "${text}" within ${timeoutMs}ms`);
}

test.describe('Co-Pilot chat behaviors', () => {
  test.use({ storageState: '.auth/sara.json' });

  // ────────────────────────────────────────────────────────────────────────
  // Test 1: Chart button only on responses that warrant chart verification
  // ────────────────────────────────────────────────────────────────────────
  //
  // ChatSurface.chartPatientIdForResponse:
  //   - briefings + medication_safety with structured data → chart button
  //   - text/query_answer → chart button ONLY when the narrative contains a
  //     CHART_VERIFY_PHRASES match ("verify in chart", "view in chart", etc.)
  //
  // We trigger one of each:
  //   (a) "Brief Marcus Webb" → briefing → chart button MUST be visible.
  //   (b) An out-of-scope chat question that the agent will refuse / answer
  //       conversationally → no chart-verify phrase → NO chart button.
  test('chart button appears on briefing but not on conversational/refused responses', async ({
    page,
  }) => {
    const frame = await openCopilot(page);
    await waitForCensus(frame);

    // Initial bubble count = 1 (the census).
    const afterBrief = await sendAndAwaitAssistant(frame, 'Brief Marcus Webb', 1, 90_000);
    expect(afterBrief).toBeGreaterThanOrEqual(2);

    // The briefing bubble is the most recent assistant bubble. The chart
    // button is rendered INSIDE that bubble.
    const briefingBubble = frame.locator(ASSISTANT_BUBBLE_SELECTOR).nth(afterBrief - 1);
    await expect(
      briefingBubble.getByRole('button', { name: /verify in chart/i }),
    ).toBeVisible({ timeout: 30_000 });

    // Now send something that the agent should treat as out-of-scope or
    // purely meta — no patient context, no clinical claims, hence no
    // CHART_VERIFY_PHRASES match. The bubble should NOT carry a chart button.
    const afterMeta = await sendAndAwaitAssistant(
      frame,
      'What is your role as an assistant?',
      afterBrief,
      60_000,
    );
    expect(afterMeta).toBeGreaterThan(afterBrief);

    // The auto-collapse effect collapses the prior briefing once this new
    // response lands — re-expand it so we can still inspect both. (Simpler:
    // just look at the LATEST bubble for the absence of the button.)
    const latestBubble = frame.locator(ASSISTANT_BUBBLE_SELECTOR).nth(afterMeta - 1);
    await expect(
      latestBubble.getByRole('button', { name: /verify in chart/i }),
    ).toHaveCount(0);
  });

  // ────────────────────────────────────────────────────────────────────────
  // Test 2: Collapsible AI responses — census is exempt from auto-collapse
  // ────────────────────────────────────────────────────────────────────────
  //
  // Effect at ChatSurface.tsx ~313: when a new finalized assistant message
  // arrives, every prior assistant bubble (except census) is added to
  // collapsedIds. The latest is removed from collapsedIds. Manual toggle
  // works on census too.
  test('new responses auto-collapse predecessors but census stays expanded', async ({ page }) => {
    const frame = await openCopilot(page);
    await waitForCensus(frame);

    // Census = 1st assistant bubble. After three more responses the surface
    // contains [census, brief, query, meds]. Per the auto-collapse rule:
    //   census  → expanded (exempt)
    //   brief   → collapsed
    //   query   → collapsed
    //   meds    → expanded (latest)
    let bubbles = 1;
    bubbles = await sendAndAwaitAssistant(frame, 'Brief Marcus Webb', bubbles, 90_000);
    bubbles = await sendAndAwaitAssistant(
      frame,
      'What is the latest potassium for Marcus Webb?',
      bubbles,
      90_000,
    );
    bubbles = await sendAndAwaitAssistant(
      frame,
      'show meds for Marcus Webb',
      bubbles,
      90_000,
    );

    // Header buttons are aria-labelled "Expand <Label> message" when
    // collapsed, "Collapse <Label> message" when expanded.
    const headers = frame.getByRole('button', { name: /(Expand|Collapse) .* message/ });
    const headerCount = await headers.count();
    expect(headerCount).toBeGreaterThanOrEqual(4);

    // First header = census; should be EXPANDED (label starts with "Collapse").
    const censusHeaderLabel = await headers.nth(0).getAttribute('aria-label');
    expect(censusHeaderLabel).toMatch(EXPANDED_HEADER_RE);
    expect(censusHeaderLabel?.toLowerCase()).toContain('census');

    // Headers 1 and 2 (brief, query) should be COLLAPSED.
    for (const idx of [1, 2]) {
      const label = await headers.nth(idx).getAttribute('aria-label');
      expect(
        label,
        `assistant bubble #${idx} expected collapsed but aria-label was "${label}"`,
      ).toMatch(COLLAPSED_HEADER_RE);
    }

    // Last header = the meds response; should be EXPANDED.
    const lastLabel = await headers.nth(headerCount - 1).getAttribute('aria-label');
    expect(lastLabel).toMatch(EXPANDED_HEADER_RE);

    // Manual collapse on census still works.
    await headers.nth(0).click();
    const censusAfterClick = await headers.nth(0).getAttribute('aria-label');
    expect(censusAfterClick).toMatch(COLLAPSED_HEADER_RE);
  });

  // ────────────────────────────────────────────────────────────────────────
  // Test 3: Census Refresh button updates the timestamp
  // ────────────────────────────────────────────────────────────────────────
  //
  // CensusRenderer renders "Census as of HH:MM" with a Refresh button next to
  // it (aria-label="Refresh census"). Clicking dispatches a
  // /triage/census?force_refresh=true and replaces the bubble's
  // generated_at, so the rendered HH:MM string updates.
  test('census Refresh updates the "Census as of HH:MM" timestamp', async ({ page }) => {
    const frame = await openCopilot(page);
    await waitForCensus(frame);

    const timestampLocator = frame.getByText(/Census as of \d{2}:\d{2}/i).first();
    await expect(timestampLocator).toBeVisible({ timeout: 30_000 });
    const before = (await timestampLocator.textContent())?.trim() ?? '';
    expect(before).toMatch(/Census as of \d{2}:\d{2}/);

    // The minute-resolution timestamp won't tick within a single test run, so
    // we need at least 60s of clock drift to guarantee a different rendered
    // value. Wait until we're at least one full minute past the captured
    // value before clicking, so the new generated_at will format differently.
    // (Skip this if it would push us past the test timeout.)
    const beforeMatch = before.match(/(\d{2}):(\d{2})/);
    if (beforeMatch) {
      const now = new Date();
      const [, hh, mm] = beforeMatch;
      const captured = new Date(now);
      captured.setHours(Number(hh), Number(mm), 0, 0);
      const elapsedMs = now.getTime() - captured.getTime();
      const waitMs = Math.max(0, 65_000 - Math.max(0, elapsedMs));
      if (waitMs > 0 && waitMs < 70_000) {
        await page.waitForTimeout(waitMs);
      }
    }

    const refreshButton = frame.getByRole('button', { name: /^Refresh census$/i });
    await refreshButton.click();

    // The Refresh button flips to "Refreshing…" then back to "Refresh"; the
    // visible HH:MM should change once the new bubble lands.
    await expect(async () => {
      const after = (await timestampLocator.textContent())?.trim() ?? '';
      expect(after).toMatch(/Census as of \d{2}:\d{2}/);
      expect(after, `before="${before}" after="${after}"`).not.toBe(before);
    }).toPass({ timeout: 30_000 });
  });

  // ────────────────────────────────────────────────────────────────────────
  // Test 4: Handoff cascade — patient blocks expand top-down with stagger
  // ────────────────────────────────────────────────────────────────────────
  //
  // HandoffRenderer enforces EXPAND_STAGGER_MS = 400ms between auto-expansions.
  // We watch the aria-expanded attribute on each patient header. When a header
  // flips from "Expand … handoff" to "Collapse … handoff" we record the
  // wall-clock time. The first three patients should expand in order with at
  // least 300ms (= 400 − 100 margin) between each pair.
  test('handoff cascade expands patients top-down with ≥300ms stagger', async ({ page }) => {
    const frame = await openCopilot(page);
    await waitForCensus(frame);

    const handoffButton = frame.getByRole('button', {
      name: /Generate shift handoff for all census patients/i,
    });
    await expect(handoffButton).toBeVisible({ timeout: 10_000 });

    // Set up the watcher BEFORE clicking — we want the first transitions.
    const watcherPromise = waitForFirstNExpansions(page, frame, 3, 90_000);

    await handoffButton.click();
    const expansions = await watcherPromise;

    expect(expansions.length, `only saw ${expansions.length}/3 expansions`).toBeGreaterThanOrEqual(3);

    // Top-down: each successive expansion should be at an index ≥ the previous.
    for (let i = 1; i < expansions.length; i++) {
      expect(
        expansions[i].headerIndex,
        `expansion order broken: ${JSON.stringify(expansions)}`,
      ).toBeGreaterThanOrEqual(expansions[i - 1].headerIndex);
    }

    // Stagger: gap between consecutive expansions ≥ 300ms.
    for (let i = 1; i < expansions.length; i++) {
      const gap = expansions[i].t - expansions[i - 1].t;
      expect(
        gap,
        `gap between handoff[${i - 1}] and handoff[${i}] was ${gap}ms (expected ≥ 300ms)`,
      ).toBeGreaterThanOrEqual(300);
    }
  });

  // ────────────────────────────────────────────────────────────────────────
  // Test 5: Markdown rendering — narrative does not leak raw "##" / "**"
  // ────────────────────────────────────────────────────────────────────────
  //
  // The Markdown component (used by every renderer) parses the narrative.
  // A free-text query response typically contains a bulleted list and bold
  // emphasis — we assert the rendered HTML uses <ul>/<strong> and the
  // user-visible innerText contains no raw markdown markers.
  test('markdown narrative renders to HTML — no raw ## or ** leaks through', async ({ page }) => {
    const frame = await openCopilot(page);
    await waitForCensus(frame);

    let bubbles = 1;
    bubbles = await sendAndAwaitAssistant(
      frame,
      'List the top three concerns for Marcus Webb as bullet points.',
      bubbles,
      90_000,
    );

    const latestBubble = frame.locator(ASSISTANT_BUBBLE_SELECTOR).nth(bubbles - 1);
    await expect(latestBubble).toBeVisible({ timeout: 30_000 });

    const innerText = (await latestBubble.innerText()).trim();
    expect(innerText.length).toBeGreaterThan(0);

    // Raw markdown markers leaking is the regression we're guarding against.
    // We allow "**" inside fenced code blocks (none expected here) — anywhere
    // else means the renderer dropped to text instead of HTML.
    expect(innerText, `innerText leaked raw "**":\n${innerText}`).not.toMatch(/\*\*/);
    expect(innerText, `innerText leaked raw "##":\n${innerText}`).not.toMatch(/(^|\n)\s*##\s/);

    // Affirmative side: rendered HTML should contain at least one of <ul>,
    // <strong>, <em>, or <li> if the model returned formatted output. We
    // settle for "any of these" because the response shape isn't guaranteed.
    const html = await latestBubble.innerHTML();
    const hasFormatting =
      /<ul[\s>]/i.test(html) ||
      /<ol[\s>]/i.test(html) ||
      /<li[\s>]/i.test(html) ||
      /<strong[\s>]/i.test(html) ||
      /<em[\s>]/i.test(html) ||
      /<h[1-6][\s>]/i.test(html);
    expect(hasFormatting, `no rendered markdown elements found in bubble HTML`).toBe(true);
  });
});

/**
 * Watch the handoff bubble for `n` expand transitions on patient headers.
 *
 * We poll every 100ms for the list of patient-header buttons inside the
 * handoff bubble and snapshot which ones are currently expanded
 * (aria-expanded="true"). Each first-time-true index is recorded with
 * Date.now(). Returns when we've seen `n` distinct expansions or `timeoutMs`
 * elapses.
 *
 * This runs entirely from Playwright's process — no instrumentation in the
 * page is required, so we don't need to add data-testid to production code.
 */
async function waitForFirstNExpansions(
  page: Page,
  frame: FrameLocator,
  n: number,
  timeoutMs: number,
): Promise<Array<{ headerIndex: number; t: number }>> {
  const headers = frame.getByRole('button', { name: /(Expand|Collapse) .+ handoff$/i });
  const seen = new Map<number, number>(); // index → first wall-clock when expanded
  const start = Date.now();
  while (Date.now() - start < timeoutMs && seen.size < n) {
    const count = await headers.count().catch(() => 0);
    for (let i = 0; i < count; i++) {
      if (seen.has(i)) continue;
      const label = await headers.nth(i).getAttribute('aria-label').catch(() => null);
      if (label && /^Collapse .+ handoff$/i.test(label)) {
        seen.set(i, Date.now());
      }
    }
    await page.waitForTimeout(100);
  }
  return [...seen.entries()]
    .map(([headerIndex, t]) => ({ headerIndex, t }))
    .sort((a, b) => a.t - b.t);
}
