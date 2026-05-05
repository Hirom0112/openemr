import { describe, test, expect } from 'vitest';
import type { ReactElement } from 'react';
import BboxOverlay, { computeOverlayRect, isUsablePolygon } from '../BboxOverlay';

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

// --- polygon precedence (Wave 2B) ---------------------------------------- #

describe('isUsablePolygon', () => {
  test('null / undefined / fewer than 3 points → false', () => {
    expect(isUsablePolygon(null)).toBe(false);
    expect(isUsablePolygon(undefined)).toBe(false);
    expect(isUsablePolygon([])).toBe(false);
    expect(isUsablePolygon([[0, 0]])).toBe(false);
    expect(isUsablePolygon([[0, 0], [10, 0]])).toBe(false);
  });

  test('three distinct points → true', () => {
    expect(isUsablePolygon([[0, 0], [10, 0], [10, 10]])).toBe(true);
  });

  test('three identical points → false (degenerate)', () => {
    expect(isUsablePolygon([[5, 5], [5, 5], [5, 5]])).toBe(false);
  });
});

describe('BboxOverlay polygon rendering (Wave 2B)', () => {
  // Cast helper — renderProps' typing assumes div, but the polygon branch
  // returns an SVG. The shape we care about is just `props` as `any`.
  function render(opts: {
    bbox?: [number, number, number, number] | null;
    polygon?: Array<[number, number]> | null;
    granularity?: 'word' | 'line';
  }): { type: string; props: Record<string, unknown> } {
    const el = BboxOverlay({
      canvasWidth: 100,
      canvasHeight: 100,
      pdfPageWidth: 100,
      pdfPageHeight: 100,
      bbox: opts.bbox ?? null,
      polygon: opts.polygon ?? null,
      granularity: opts.granularity,
    }) as { type: string; props: Record<string, unknown> } | null;
    if (!el) throw new Error('BboxOverlay returned null');
    return el;
  }

  test('renders SVG polygon when polygon present (precedence over bbox)', () => {
    const el = render({
      bbox: [0, 0, 10, 10],
      polygon: [
        [0, 0],
        [50, 0],
        [50, 50],
        [0, 50],
      ],
    });
    expect(el.type).toBe('svg');
    expect((el.props as { ['data-shape']: string })['data-shape']).toBe('polygon');
    // Inspect <polygon> child's points attr.
    const child = (el.props as { children: { props: { points: string } } }).children;
    expect(child.props.points).toBe('0,0 50,0 50,50 0,50');
  });

  test('renders bbox div when polygon is null', () => {
    const el = render({ bbox: [0, 0, 10, 10], polygon: null });
    expect(el.type).toBe('div');
    expect((el.props as { ['data-shape']: string })['data-shape']).toBe('bbox');
  });

  test('renders bbox div when polygon is degenerate (<3 distinct points)', () => {
    const el = render({
      bbox: [0, 0, 10, 10],
      polygon: [
        [0, 0],
        [10, 0],
      ],
    });
    expect(el.type).toBe('div');
    expect((el.props as { ['data-shape']: string })['data-shape']).toBe('bbox');
  });

  test('scales polygon points by canvas/page ratio', () => {
    const el = BboxOverlay({
      canvasWidth: 200,
      canvasHeight: 400,
      pdfPageWidth: 100,
      pdfPageHeight: 100,
      bbox: null,
      polygon: [
        [10, 20],
        [50, 20],
        [50, 60],
      ],
    }) as { props: { children: { props: { points: string } } } };
    // sx=2, sy=4 → (10,20)->(20,80), (50,20)->(100,80), (50,60)->(100,240)
    expect(el.props.children.props.points).toBe('20,80 100,80 100,240');
  });
});
