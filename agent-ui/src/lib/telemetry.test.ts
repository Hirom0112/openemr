import { describe, test, expect, beforeEach } from 'vitest';
import { bucketSize, flush, hashFilename, peek, record } from './telemetry';

describe('telemetry shim (Slice 9.8)', () => {
  beforeEach(() => {
    flush();
  });

  test('records and drains events', () => {
    record({ name: 'dropzone_validate', outcome: 'success' });
    record({ name: 'dropzone_upload_start' });
    expect(peek()).toHaveLength(2);
    const drained = flush();
    expect(drained.map((e) => e.name)).toEqual(['dropzone_validate', 'dropzone_upload_start']);
    expect(peek()).toHaveLength(0);
  });

  test('bucketSize buckets correctly', () => {
    expect(bucketSize(0)).toBe('lt_1mb');
    expect(bucketSize(2 * 1024 * 1024)).toBe('1_5mb');
    expect(bucketSize(10 * 1024 * 1024)).toBe('5_25mb');
    expect(bucketSize(30 * 1024 * 1024)).toBe('gt_25mb');
  });

  test('hashFilename returns 12 hex chars (deterministic for same input)', async () => {
    const a = await hashFilename('lab_report.pdf');
    const b = await hashFilename('lab_report.pdf');
    expect(a).toHaveLength(12);
    expect(/^[0-9a-f]{12}$/.test(a)).toBe(true);
    expect(a).toBe(b);
    const c = await hashFilename('different.pdf');
    expect(c).not.toBe(a);
  });
});
