import type { KeyboardEvent, ReactElement } from 'react';
import type { Citation } from '../types/citation';
import { BRAND, SURFACE } from '../styles/tokens';

/**
 * Clickable pill that opens the corresponding citation in the document
 * viewer. Renders e.g. `[2/4] OSH Lab Fax · p2` for a document citation.
 *
 * Keyboard-accessible (Enter / Space activate). `index`/`total` are
 * optional — when both are present we render a `[i/n]` prefix so doctors
 * can tell at a glance how many sources back a fact.
 */

export interface CitationChipProps {
  citation: Citation;
  onClick: (citation: Citation) => void;
  index?: number;
  total?: number;
}

function describeLocation(citation: Citation): string {
  const loc = citation.page_or_section;
  if (!loc) return '';
  if (citation.source_type === 'document') {
    // page_or_section is "2" (raw page) or "p2" — normalize to "p2"
    return /^\d+$/.test(loc) ? `p${loc}` : loc;
  }
  if (citation.source_type === 'guideline') {
    return loc.startsWith('§') ? loc : `§${loc}`;
  }
  return loc;
}

function describeSource(citation: Citation): string {
  // source_id can be opaque (UUID) or human-friendly. Fall back to
  // source_type when the id looks like an opaque token.
  const id = citation.source_id;
  if (!id) return citation.source_type;
  if (id.length > 24 && /^[0-9a-fA-F-]+$/.test(id)) {
    return citation.source_type === 'document' ? 'Document' :
      citation.source_type === 'guideline' ? 'Guideline' : 'Observation';
  }
  return id;
}

export default function CitationChip(props: CitationChipProps): ReactElement {
  const { citation, onClick, index, total } = props;
  const source = describeSource(citation);
  const loc = describeLocation(citation);
  const counter = (typeof index === 'number' && typeof total === 'number' && total > 1)
    ? `[${index + 1}/${total}] `
    : '';
  // Prefer the human-readable per-fact label (e.g. "Sodium 138 mEq/L") when
  // the renderer attached one. Falls back to the source id so older callers
  // and non-document citations continue to render as before.
  const head = (citation.label && citation.label.trim().length > 0)
    ? citation.label
    : source;
  const label = `${counter}${head}${loc ? ` · ${loc}` : ''}`;
  const ariaLabel = `Open citation ${counter}from ${head}${loc ? `, ${loc}` : ''}`;

  const handleKeyDown = (e: KeyboardEvent<HTMLButtonElement>): void => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onClick(citation);
    }
  };

  return (
    <button
      type="button"
      role="button"
      aria-label={ariaLabel}
      onClick={() => onClick(citation)}
      onKeyDown={handleKeyDown}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        padding: '3px 8px',
        margin: '2px 4px 2px 0',
        background: SURFACE.panel,
        border: `1px solid ${BRAND.base}`,
        color: BRAND.base,
        borderRadius: 12,
        fontSize: 11,
        fontWeight: 500,
        fontFamily: 'inherit',
        cursor: 'pointer',
        lineHeight: 1.4,
        whiteSpace: 'nowrap',
      }}
    >
      <span aria-hidden="true">¶</span>
      <span>{label}</span>
    </button>
  );
}
