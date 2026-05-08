/**
 * Phase-3 Documents tab — DOCX paragraph viewer.
 *
 * Renders a list of styled text paragraphs (the DOCX parser's output) with
 * an optional highlighted paragraph that the citation chip points to. The
 * caller supplies the paragraph list — for v1 this is typically the
 * citations.quote_or_value strings drawn from the row payloads (see
 * DocumentReviewPanel.tsx for the synthesizer). The full DOCX paragraph
 * fetch can be wired later behind the same prop interface.
 */

import { useEffect, useRef, type ReactElement } from 'react';
import { BRAND, SURFACE } from '../styles/tokens';

export interface DocxParagraphsViewerProps {
  paragraphs: string[];
  highlightedIndex: number | null;
  onParagraphClick?: (index: number) => void;
}

export default function DocxParagraphsViewer(p: DocxParagraphsViewerProps): ReactElement {
  const refs = useRef<Array<HTMLDivElement | null>>([]);

  // Scroll the highlighted paragraph into view whenever it changes. Center
  // alignment so a paragraph at the top of a long list still gets context
  // from its neighbours.
  useEffect(() => {
    if (p.highlightedIndex == null) return;
    const el = refs.current[p.highlightedIndex];
    if (el && typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }, [p.highlightedIndex]);

  if (p.paragraphs.length === 0) {
    return (
      <div style={emptyStyle}>
        Source preview unavailable in v1; use citation chips for context.
      </div>
    );
  }

  return (
    <div style={containerStyle}>
      {p.paragraphs.map((text, idx) => {
        const isHl = p.highlightedIndex === idx;
        const style: React.CSSProperties = {
          ...paragraphStyle,
          background: isHl ? BRAND.tint : 'transparent',
          borderLeft: isHl ? `3px solid ${BRAND.base}` : '3px solid transparent',
          cursor: p.onParagraphClick ? 'pointer' : 'default',
        };
        return (
          <div
            key={idx}
            ref={(el) => { refs.current[idx] = el; }}
            style={style}
            onClick={p.onParagraphClick ? () => p.onParagraphClick?.(idx) : undefined}
            data-paragraph-index={idx}
          >
            {text || <span style={{ color: SURFACE.subtle, fontStyle: 'italic' }}>(empty)</span>}
          </div>
        );
      })}
    </div>
  );
}

const containerStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 6,
  padding: 14,
  background: SURFACE.bg,
  fontSize: 13,
  lineHeight: 1.55,
  color: SURFACE.fg,
};

const paragraphStyle: React.CSSProperties = {
  padding: '6px 10px',
  borderRadius: 4,
  whiteSpace: 'pre-wrap',
  transition: 'background 0.15s',
};

const emptyStyle: React.CSSProperties = {
  padding: 24,
  color: SURFACE.muted,
  fontSize: 12,
  fontStyle: 'italic',
};
