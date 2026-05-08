import { useState, type ReactElement, type CSSProperties, type KeyboardEvent } from 'react';
import { BRAND } from '../styles/tokens';

/**
 * Clickable wrapper around the post-ingest citation-chip aesthetic. Used by
 * `PostIngestContextCard` and the chat-narrative token parser to render
 * synthesis citation tokens (`fact:obs:*`, `fact:intake:*`, `guideline:*`)
 * as keyboard-accessible buttons.
 *
 * Visual identity matches `CITATION_CHIP_STYLE` in `PostIngestContextCard`
 * (BRAND.base background, monospace, small radius). We add cursor:pointer,
 * hover/focus rings, and button semantics so the chip is a real affordance.
 *
 * Distinct from `CitationChip.tsx` — that component takes a fully-shaped
 * `Citation` object (with bbox + page metadata) for the W2 document viewer.
 * This wrapper only carries the raw token id and delegates click handling.
 */
export interface SynthesisCitationChipProps {
  /** Raw token id, e.g. "fact:obs:42", "fact:intake:Lisinopril_dose", "guideline:abc". */
  citationId: string;
  onClick: (id: string) => void;
  /** Optional hover tooltip (used for guideline chunk_id). */
  title?: string;
}

const BASE_STYLE: CSSProperties = {
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
  fontSize: 10,
  padding: '1px 5px',
  borderRadius: 4,
  background: BRAND.base,
  color: BRAND.onBrand,
  marginLeft: 4,
  whiteSpace: 'nowrap',
  border: 'none',
  cursor: 'pointer',
  display: 'inline-block',
  lineHeight: 1.4,
  verticalAlign: 'baseline',
};

const HOVER_STYLE: CSSProperties = {
  filter: 'brightness(1.15)',
};

const FOCUS_STYLE: CSSProperties = {
  outline: `2px solid ${BRAND.base}`,
  outlineOffset: 1,
};

export default function SynthesisCitationChip(
  props: SynthesisCitationChipProps,
): ReactElement {
  const { citationId, onClick, title } = props;
  const [hover, setHover] = useState(false);
  const [focus, setFocus] = useState(false);

  const handleKey = (e: KeyboardEvent<HTMLButtonElement>): void => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onClick(citationId);
    }
  };

  return (
    <button
      type="button"
      onClick={() => onClick(citationId)}
      onKeyDown={handleKey}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onFocus={() => setFocus(true)}
      onBlur={() => setFocus(false)}
      title={title}
      aria-label={`Open citation ${citationId}`}
      style={{
        ...BASE_STYLE,
        ...(hover ? HOVER_STYLE : null),
        ...(focus ? FOCUS_STYLE : null),
      }}
    >
      {citationId}
    </button>
  );
}
