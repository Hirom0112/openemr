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
  color?: string;
  /**
   * Optional granularity hint from the OCR layout block. Renders a dashed
   * border for word-level citations and a solid border for line-level (or
   * unspecified) citations — a subtle visual affordance, no geometry change.
   */
  granularity?: 'word' | 'line';
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
    canvasWidth,
    canvasHeight,
    pdfPageWidth,
    pdfPageHeight,
    color = '#f97316',
    granularity,
  } = props;
  if (!bbox || !pdfPageWidth || !pdfPageHeight) return null;
  const rect = computeOverlayRect(bbox, canvasWidth, canvasHeight, pdfPageWidth, pdfPageHeight);
  // Word-level boxes get a dashed border to signal "tighter, narrower
  // citation"; line and undefined granularity keep the existing solid border.
  const borderStyle = granularity === 'word' ? 'dashed' : 'solid';
  return (
    <div
      data-testid="bbox-overlay"
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
