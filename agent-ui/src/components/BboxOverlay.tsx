import type { ReactElement } from 'react';

/**
 * Translates a PDF-space bbox into a screen-space rectangle and draws a
 * 2px outlined div over the absolutely-positioned canvas. A `<div>` is enough
 * here — a second canvas would buy us nothing for a single rectangle and
 * makes the geometry harder to assert in tests.
 */

export interface BboxOverlayProps {
  /** Rendered canvas size in CSS pixels. */
  canvasWidth: number;
  canvasHeight: number;
  /** PDF page intrinsic size in points, taken from page.getViewport({ scale: 1 }). */
  pdfPageWidth: number;
  pdfPageHeight: number;
  /** [x, y, w, h] in PDF points. Origin top-left. Null = nothing to draw. */
  bbox: [number, number, number, number] | null;
  /**
   * Optional polygon (Wave 2B) — list of [x, y] pairs in the SAME coord
   * frame as `bbox`. Polygon precedence: when present AND non-degenerate
   * (≥3 distinct points) we render an SVG `<polygon>` rather than the
   * `<div>` bbox. Falls back to bbox otherwise.
   */
  polygon?: Array<[number, number]> | null;
  color?: string;
  /**
   * Optional granularity hint from the OCR layout block. Renders a dashed
   * border for word-level citations and a solid border for line-level (or
   * unspecified) citations — a subtle visual affordance, no geometry change.
   */
  granularity?: 'word' | 'line';
}

/** True iff `poly` has ≥3 distinct points — i.e. encloses a region. */
export function isUsablePolygon(
  poly: Array<[number, number]> | null | undefined,
): poly is Array<[number, number]> {
  if (!poly || poly.length < 3) return false;
  const seen = new Set<string>();
  for (const [x, y] of poly) {
    seen.add(`${x},${y}`);
    if (seen.size >= 3) return true;
  }
  return false;
}

export function computeOverlayRect(
  bbox: [number, number, number, number],
  canvasWidth: number,
  canvasHeight: number,
  pdfPageWidth: number,
  pdfPageHeight: number,
): { left: number; top: number; width: number; height: number } {
  const sx = canvasWidth / pdfPageWidth;
  const sy = canvasHeight / pdfPageHeight;
  const [x, y, w, h] = bbox;
  return {
    left: x * sx,
    top: y * sy,
    width: w * sx,
    height: h * sy,
  };
}

export default function BboxOverlay(props: BboxOverlayProps): ReactElement | null {
  const {
    bbox,
    polygon,
    canvasWidth,
    canvasHeight,
    pdfPageWidth,
    pdfPageHeight,
    color = '#f97316',
    granularity,
  } = props;
  if (!pdfPageWidth || !pdfPageHeight) return null;

  // Polygon precedence (Wave 2B contract §1): when polygon is present
  // AND non-degenerate it wins over bbox. We render an SVG layer rather
  // than a div so the shape is faithful (rotated text, paddle line
  // polygons, etc.) — the geometry math reuses the same sx/sy scale.
  if (isUsablePolygon(polygon)) {
    const sx = canvasWidth / pdfPageWidth;
    const sy = canvasHeight / pdfPageHeight;
    const points = polygon
      .map(([x, y]) => `${x * sx},${y * sy}`)
      .join(' ');
    const strokeStyle = granularity === 'word' ? '4 3' : undefined;
    return (
      <svg
        data-testid="bbox-overlay"
        data-shape="polygon"
        data-granularity={granularity ?? 'unknown'}
        aria-hidden="true"
        width={canvasWidth}
        height={canvasHeight}
        style={{
          position: 'absolute',
          left: 0,
          top: 0,
          pointerEvents: 'none',
          overflow: 'visible',
        }}
      >
        <polygon
          points={points}
          fill="none"
          stroke={color}
          strokeWidth={2}
          strokeDasharray={strokeStyle}
          // Subtle white halo so the polygon stands out on dark/light docs alike.
          // Mirrors the boxShadow trick used on the bbox div.
          style={{ filter: 'drop-shadow(0 0 1px rgba(255,255,255,0.6))' }}
        />
      </svg>
    );
  }

  if (!bbox) return null;
  const rect = computeOverlayRect(bbox, canvasWidth, canvasHeight, pdfPageWidth, pdfPageHeight);
  // Word-level boxes get a dashed border to signal "tighter, narrower
  // citation"; line and undefined granularity keep the existing solid border.
  const borderStyle = granularity === 'word' ? 'dashed' : 'solid';
  return (
    <div
      data-testid="bbox-overlay"
      data-shape="bbox"
      data-granularity={granularity ?? 'unknown'}
      aria-hidden="true"
      style={{
        position: 'absolute',
        left: rect.left,
        top: rect.top,
        width: rect.width,
        height: rect.height,
        border: `2px ${borderStyle} ${color}`,
        borderRadius: 2,
        pointerEvents: 'none',
        boxShadow: `0 0 0 1px rgba(255,255,255,0.6)`,
      }}
    />
  );
}
