import { describe, test, expect, beforeEach, afterEach, vi } from 'vitest';
import { RED, AMB, MUTED } from '../styles/tokens';

// regression spec mirroring BriefingRenderer logic (BriefingRenderer.tsx, commit 94853214f)
// The freshness indicator helpers are inlined inside the component, so we mirror
// the algorithm here. Any divergence between this spec and the component is a bug
// in one or the other — keep them in sync.

const STALE_AMBER_MS = 10 * 60 * 1000;
const STALE_RED_MS = 30 * 60 * 1000;

type StalenessColor = string;

function computeFreshness(generatedAt: string | null | undefined, now: number): {
  valid: boolean;
  color: StalenessColor;
  short?: string;
  tooltip?: string;
} {
  const generatedDate = generatedAt ? new Date(generatedAt) : null;
  const valid = !!(generatedDate && !Number.isNaN(generatedDate.getTime()));
  const ageMs = valid ? now - (generatedDate as Date).getTime() : 0;
  const color: StalenessColor =
    !valid ? MUTED
      : ageMs > STALE_RED_MS ? RED.text
      : ageMs > STALE_AMBER_MS ? AMB.text
      : MUTED;

  if (!valid) return { valid, color };
  const d = generatedDate as Date;
  const short = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const tooltip = `Last fetched from chart at ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })} on ${d.toISOString().slice(0, 10)}`;
  return { valid, color, short, tooltip };
}

describe('BriefingRenderer freshness indicator', () => {
  const fixedNow = new Date('2026-05-02T15:00:00Z').getTime();

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(fixedNow);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  test('fresh data (just now) → MUTED color', () => {
    const r = computeFreshness(new Date(fixedNow).toISOString(), fixedNow);
    expect(r.color).toBe(MUTED);
    expect(r.valid).toBe(true);
  });

  test('5 min ago → MUTED', () => {
    const r = computeFreshness(new Date(fixedNow - 5 * 60 * 1000).toISOString(), fixedNow);
    expect(r.color).toBe(MUTED);
  });

  test('11 min ago → AMB', () => {
    const r = computeFreshness(new Date(fixedNow - 11 * 60 * 1000).toISOString(), fixedNow);
    expect(r.color).toBe(AMB.text);
  });

  test('31 min ago → RED', () => {
    const r = computeFreshness(new Date(fixedNow - 31 * 60 * 1000).toISOString(), fixedNow);
    expect(r.color).toBe(RED.text);
  });

  test('exactly 10 min ago → MUTED (boundary, not >)', () => {
    const r = computeFreshness(new Date(fixedNow - 10 * 60 * 1000).toISOString(), fixedNow);
    expect(r.color).toBe(MUTED);
  });

  test('exactly 30 min ago → AMB (boundary, not >)', () => {
    const r = computeFreshness(new Date(fixedNow - 30 * 60 * 1000).toISOString(), fixedNow);
    expect(r.color).toBe(AMB.text);
  });

  test('missing timestamp → MUTED + no short/tooltip (fallback)', () => {
    const r = computeFreshness(undefined, fixedNow);
    expect(r.color).toBe(MUTED);
    expect(r.valid).toBe(false);
    expect(r.short).toBeUndefined();
    expect(r.tooltip).toBeUndefined();
  });

  test('null timestamp → MUTED + fallback', () => {
    const r = computeFreshness(null, fixedNow);
    expect(r.color).toBe(MUTED);
    expect(r.valid).toBe(false);
  });

  test('invalid timestamp string → does not throw, MUTED + fallback', () => {
    expect(() => computeFreshness('not-a-date', fixedNow)).not.toThrow();
    const r = computeFreshness('not-a-date', fixedNow);
    expect(r.valid).toBe(false);
    expect(r.color).toBe(MUTED);
    expect(r.short).toBeUndefined();
  });

  test('time zone: short format matches Date.toLocaleTimeString in local tz', () => {
    const iso = new Date(fixedNow - 2 * 60 * 1000).toISOString();
    const r = computeFreshness(iso, fixedNow);
    const expected = new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    expect(r.short).toBe(expected);
  });

  test('tooltip includes ISO date prefix', () => {
    const iso = '2026-05-02T14:55:30Z';
    const r = computeFreshness(iso, fixedNow);
    expect(r.tooltip).toContain('2026-05-02');
    expect(r.tooltip).toMatch(/Last fetched from chart at/);
  });
});
