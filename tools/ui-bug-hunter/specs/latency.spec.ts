import { test } from '@playwright/test';
import { openCopilot, waitForCensus } from './_helpers';

/**
 * Latency probe for the agent backend endpoints exposed via the Co-Pilot iframe.
 *
 * This is a SOFT-ASSERT spec — it never fails the suite on slow responses.
 * It just measures p95 / mean / stdev for /agent, /triage, /briefing and
 * attaches the numbers to the test report. Issue 5 will tighten thresholds
 * after we have a few baseline runs in CI.
 */

interface Sample {
  endpoint: string;
  ms: number;
}

function summarize(samples: number[]): { mean: number; stdev: number; p95: number } {
  if (samples.length === 0) return { mean: 0, stdev: 0, p95: 0 };
  const sorted = [...samples].sort((a, b) => a - b);
  const mean = sorted.reduce((a, b) => a + b, 0) / sorted.length;
  const variance = sorted.reduce((acc, v) => acc + (v - mean) ** 2, 0) / sorted.length;
  const stdev = Math.sqrt(variance);
  const p95Index = Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95));
  return { mean, stdev, p95: sorted[p95Index] ?? 0 };
}

test.describe('Co-Pilot agent latency (soft-assert)', () => {
  test.use({ storageState: '.auth/sara.json' });

  test('record p95/mean/stdev for /agent, /triage, /briefing', async ({ page }) => {
    const samples: Sample[] = [];
    const inflight = new Map<string, number>();

    page.on('request', (req) => {
      const url = req.url();
      if (/\/(agent|triage|briefing)(\b|\/|\?)/.test(url)) {
        inflight.set(url + req.method() + (req.postData() ?? ''), Date.now());
      }
    });
    page.on('response', async (res) => {
      const req = res.request();
      const key = res.url() + req.method() + (req.postData() ?? '');
      const started = inflight.get(key);
      if (started === undefined) return;
      inflight.delete(key);
      const url = res.url();
      let endpoint = 'other';
      if (/\/agent(\b|\/|\?)/.test(url)) endpoint = 'agent';
      else if (/\/triage(\b|\/|\?)/.test(url)) endpoint = 'triage';
      else if (/\/briefing(\b|\/|\?)/.test(url)) endpoint = 'briefing';
      samples.push({ endpoint, ms: Date.now() - started });
    });

    // 5x census reloads — exercises /triage (and /agent on first paint).
    for (let i = 0; i < 5; i++) {
      const frame = await openCopilot(page);
      await waitForCensus(frame);
      await page.reload({ waitUntil: 'domcontentloaded' });
    }

    // 5x briefings on different patients (triggered via chat input).
    {
      const frame = await openCopilot(page);
      await waitForCensus(frame);
      const input = frame.getByPlaceholder(/Ask about a patient/i);
      const names = [
        'Brief Marcus Webb',
        'Brief Delia Fontaine',
        'Brief Alejandro Cruz',
        'Brief Priya Anand',
        'Brief Raymond Okafor',
      ];
      for (const q of names) {
        await input.fill(q);
        await input.press('Enter');
        // Best-effort wait for the response to come back.
        await page.waitForTimeout(2_000);
      }
    }

    // 5x free-text chat queries — should hit /agent or /query.
    {
      const frame = await openCopilot(page);
      await waitForCensus(frame);
      const input = frame.getByPlaceholder(/Ask about a patient/i);
      const queries = [
        'What is the most concerning vital today?',
        'Who is the highest priority patient right now?',
        'Summarize the abnormal labs cluster',
        'Are there any open code statuses?',
        'What changed in the last hour?',
      ];
      for (const q of queries) {
        await input.fill(q);
        await input.press('Enter');
        await page.waitForTimeout(2_000);
      }
    }

    const byEndpoint = new Map<string, number[]>();
    for (const s of samples) {
      const arr = byEndpoint.get(s.endpoint) ?? [];
      arr.push(s.ms);
      byEndpoint.set(s.endpoint, arr);
    }

    const report: Record<string, { count: number; mean: number; stdev: number; p95: number }> = {};
    for (const [endpoint, arr] of byEndpoint) {
      report[endpoint] = { count: arr.length, ...summarize(arr) };
    }

    // eslint-disable-next-line no-console
    console.log('[ui-bug-hunter] latency report:', JSON.stringify(report, null, 2));
    await test.info().attach('latency.json', {
      body: JSON.stringify(report, null, 2),
      contentType: 'application/json',
    });

    // Soft assertion only: warn if any endpoint had zero samples.
    if (Object.keys(report).length === 0) {
      test.info().annotations.push({
        type: 'warning',
        description: 'No /agent, /triage, or /briefing requests were observed.',
      });
    }
  });
});
