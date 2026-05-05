import { describe, test, expect } from 'vitest';
import type { ReactElement } from 'react';
import BboxOverlay, { computeOverlayRect } from '../BboxOverlay';

// No DOM here — the file follows the project's existing pure-logic test
// style (see ChatSurface.test.ts, BriefingRenderer.test.ts). The pixel math
// is the part of BboxOverlay that's worth pinning; the JSX is a one-liner
// styled div the bundler will catch if it breaks.

describe('computeOverlayRect (BboxOverlay)', () => {
  test('1:1 mapping when canvas size equals PDF page size', () => {
    const r = computeOverlayRect([10, 20, 100, 50], 612, 792, 612, 792);
    expect(r).toEqual({ left: 10, top: 20, width: 100, height: 50 });
  });

  test('scales linearly when canvas is 2x the page width/height', () => {
    const r = computeOverlayRect([10, 20, 100, 50], 1224, 1584, 612, 792);
    expect(r).toEqual({ left: 20, top: 40, width: 200, height: 100 });
  });

  test('scales independently on each axis', () => {
    const r = computeOverlayRect([0, 0, 50, 50], 1224, 792, 612, 792);
    // x doubles, y unchanged
    expect(r).toEqual({ left: 0, top: 0, width: 100, height: 50 });
  });

  test('non-zero offset translates correctly under scaling', () => {
    const r = computeOverlayRect([100, 200, 50, 25], 306, 396, 612, 792);
    // 0.5x on both axes
    expect(r).toEqual({ left: 50, top: 100, width: 25, height: 12.5 });
  });
});

// --- granularity affordance ---------------------------------------------
//
// These tests exercise BboxOverlay's render output without a DOM by inspecting
// the returned ReactElement's props directly. No jsdom/testing-library is
// configured for this package, so we keep with the same pure-logic style.

interface OverlayProps {
  style: { border: string };
  ['data-granularity']: string;
}

function renderProps(granularity?: 'word' | 'line'): OverlayProps {
  const el = BboxOverlay({
    canvasWidth: 100,
    canvasHeight: 100,
    pdfPageWidth: 100,
    pdfPageHeight: 100,
    bbox: [0, 0, 10, 10],
    granularity,
  }) as ReactElement;
  return el.props as OverlayProps;
}

describe('BboxOverlay granularity affordance', () => {
  test('word granularity renders a dashed border', () => {
    const props = renderProps('word');
    expect(props.style.border).toContain('dashed');
    expect(props['data-granularity']).toBe('word');
  });

  test('line granularity keeps the solid border', () => {
    const props = renderProps('line');
    expect(props.style.border).toContain('solid');
    expect(props['data-granularity']).toBe('line');
  });

  test('undefined granularity falls back to solid border', () => {
    const props = renderProps(undefined);
    expect(props.style.border).toContain('solid');
    expect(props['data-granularity']).toBe('unknown');
  });
});
