/**
 * Slice 9.8 — lane indicator chip.
 *
 * Renders a small color-coded pill identifying which multimodal lane a file
 * (or a staging batch) belongs to. Used in two surfaces:
 *
 *   1. FileDropZone — pre-upload classification preview beneath the pill.
 *   2. ApprovalModal / QuarantineCard headers — confirms which pipeline
 *      produced the rows you're approving / triaging.
 *
 * Lane → color mapping is centralized in `styles/tokens.ts::LANE`.
 */

import type { ReactElement } from 'react';
import { LANE, NEU, type Lane } from '../styles/tokens';

const EXT_TO_LANE: Record<string, Lane> = {
  pdf: 'DOCUMENT',
  png: 'DOCUMENT',
  tif: 'DOCUMENT',
  tiff: 'DOCUMENT',
  docx: 'DOCUMENT',
  hl7: 'HL7',
  xlsx: 'WORKBOOK',
};

/** Best-effort lane classification from a filename. ``null`` if no extension match. */
export function laneFromFilename(name: string): Lane | null {
  const lower = name.toLowerCase();
  const dot = lower.lastIndexOf('.');
  if (dot < 0) return null;
  const ext = lower.slice(dot + 1);
  return EXT_TO_LANE[ext] ?? null;
}

export interface LaneChipProps {
  lane: Lane | null;
  size?: 'sm' | 'md';
  /** Override the default short label (e.g. "Workbook (XLSX)"). */
  label?: string;
}

export default function LaneChip(props: LaneChipProps): ReactElement {
  const { lane, size = 'sm', label } = props;
  const tok = lane ? LANE[lane] : { bg: NEU.bg, border: NEU.border, text: NEU.text, label: 'Unknown' };
  const fontSize = size === 'sm' ? 10 : 12;
  const padding = size === 'sm' ? '1px 7px' : '2px 10px';
  return (
    <span
      aria-label={`Lane: ${label ?? tok.label}`}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        background: tok.bg,
        border: `1px solid ${tok.border}`,
        color: tok.text,
        fontSize,
        fontWeight: 600,
        letterSpacing: '0.03em',
        textTransform: 'uppercase',
        borderRadius: 4,
        padding,
        lineHeight: 1.2,
        userSelect: 'none',
      }}
    >
      {label ?? tok.label}
    </span>
  );
}
