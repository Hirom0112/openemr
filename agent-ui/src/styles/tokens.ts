import type React from 'react';

export const RED = { bg: '#FCEBEB', border: '#E24B4A', text: '#7F1D1D', secondary: '#991B1B' };
export const AMB = { bg: '#FAEEDA', border: '#EF9F27', text: '#78350F', secondary: '#92400E' };
export const NEU = { bg: '#F8F9FA', border: '#E5E7EB', text: '#374151', secondary: '#6B7280' };
export const GREEN_PILL = { bg: '#DCFCE7', border: '#86EFAC', text: '#166534' };
export const MUTED = '#6B7280';
export const LINK_TINT = '#E8EDF8';

export const BRAND = {
  base: '#1E3A8A',
  hover: '#1E40AF',
  tint: '#EEF2FF',
  onBrand: '#FFFFFF',
};

// Slice 9.8 — lane indicator tokens. Each lane in the multimodal pipeline
// (DOCUMENT/HL7/WORKBOOK) gets its own color so the LaneChip is identifiable
// at a glance in the dropzone, ApprovalModal header, and quarantine card.
// Closed enum — anything outside this set falls back to NEU in LaneChip.
export type Lane = 'DOCUMENT' | 'HL7' | 'WORKBOOK';

export const LANE: Record<Lane, { bg: string; border: string; text: string; label: string }> = {
  DOCUMENT: { bg: BRAND.tint,   border: BRAND.base, text: BRAND.base,  label: 'Document' },
  HL7:      { bg: '#F3E8FF',    border: '#7E22CE',  text: '#581C87',   label: 'HL7' },
  WORKBOOK: { bg: '#CCFBF1',    border: '#0F766E',  text: '#134E4A',   label: 'Workbook' },
};

export const SURFACE = {
  bg: '#FFFFFF',
  panel: '#F9FAFB',
  hover: '#F3F4F6',
  border: '#E5E7EB',
  borderStrong: '#D1D5DB',
  fg: '#111827',
  fgStrong: '#0F172A',
  muted: '#6B7280',
  subtle: '#9CA3AF',
};

export type ColorToken = typeof RED;

export function claimRowStyle(color: ColorToken): React.CSSProperties {
  return {
    background: color.bg,
    border: `1px solid ${color.border}`,
    borderRadius: 6,
    padding: '6px 10px',
    marginBottom: 3,
    lineHeight: 1.5,
  };
}

export function cardStyle(color: ColorToken): React.CSSProperties {
  return {
    border: `1px solid ${color.border}`,
    borderRadius: 8,
    padding: '10px 12px',
    background: color.bg,
  };
}

export function primaryButtonStyle(color: ColorToken): React.CSSProperties {
  return { background: '#fff', color: color.text, border: `1px solid ${color.border}` };
}

export function secondaryButtonStyle(): React.CSSProperties {
  return { background: LINK_TINT, color: NEU.text, border: `1px solid ${NEU.border}` };
}

export function footerDividerStyle(): React.CSSProperties {
  return { borderTop: `1px solid ${NEU.border}`, marginTop: 16, paddingTop: 10, fontSize: 11, color: MUTED };
}

export function sectionHeadingStyle(color: ColorToken): React.CSSProperties {
  return { textTransform: 'uppercase', fontSize: 11, fontWeight: 600, letterSpacing: '0.06em', color: color.text };
}

// Promoted census-section heading: larger, mixed-case, weighty. Pairs with a
// leading TierDot so the eye lands on the section anchor before the rows.
// Replaces the older uppercase 11px label inside CensusRenderer.
export function censusSectionHeadingStyle(color: ColorToken): React.CSSProperties {
  return {
    fontSize: 14,
    fontWeight: 600,
    letterSpacing: '-0.005em',
    color: color === NEU ? SURFACE.fg : color.text,
    margin: 0,
    lineHeight: 1.3,
  };
}

// Tier chip used at the leading edge of a patient row (e.g. "P2" / "P3").
// Carries the tier color visibly so triage rank reads at a glance — the
// "triage-first hierarchy" principle made literal.
export function tierChipStyle(color: ColorToken): React.CSSProperties {
  return {
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    minWidth: 30,
    height: 22,
    padding: '0 7px',
    borderRadius: 4,
    background: color.border,
    color: color === NEU ? SURFACE.fg : '#FFFFFF',
    fontSize: 11,
    fontWeight: 700,
    letterSpacing: '0.02em',
    flexShrink: 0,
    fontVariantNumeric: 'tabular-nums',
  };
}

export function summaryCardStyle(color: ColorToken): React.CSSProperties {
  return {
    background: color.bg,
    border: `1px solid ${color.border}`,
    borderRadius: 8,
    padding: '8px 10px',
    textAlign: 'center',
  };
}

export function metricCardStyle(color: ColorToken): React.CSSProperties {
  return summaryCardStyle(color);
}

export function patientRowStyle(color: ColorToken): React.CSSProperties {
  return {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    padding: '8px 12px',
    borderRadius: 6,
    background: color.bg,
    border: `1px solid ${color.border}`,
    marginBottom: 4,
  };
}

export function livePillStyle(): React.CSSProperties {
  return {
    fontSize: 11,
    fontWeight: 500,
    color: GREEN_PILL.text,
    background: GREEN_PILL.bg,
    border: `1px solid ${GREEN_PILL.border}`,
    borderRadius: 999,
    padding: '3px 10px',
    display: 'flex',
    alignItems: 'center',
    gap: 5,
  };
}

export function admitBadgeStyle(badge: { bg: string; fg: string; border: string }): React.CSSProperties {
  return {
    fontSize: 10,
    fontWeight: 500,
    padding: '1px 6px',
    borderRadius: 999,
    background: badge.bg,
    color: badge.fg,
    border: `1px solid ${badge.border}`,
    flexShrink: 0,
  };
}

export type Severity = 'critical' | 'high' | 'moderate' | 'normal' | 'info';

export function severityColor(level: Severity): { dot: string; text: string; accent: string } {
  switch (level) {
    case 'critical':
      return { dot: RED.border, text: RED.text, accent: RED.bg };
    case 'high':
      return { dot: AMB.border, text: AMB.text, accent: AMB.bg };
    case 'moderate':
      return { dot: '#F4C77B', text: AMB.secondary, accent: AMB.bg };
    case 'normal':
      return { dot: NEU.border, text: NEU.secondary, accent: NEU.bg };
    case 'info':
    default:
      return { dot: SURFACE.subtle, text: NEU.text, accent: NEU.bg };
  }
}

// Type scale: 4 steps, ratio ~1.25 between adjacent steps. Weight contrast does
// the rest of the work. Clinical UI — the smallest step still needs to be
// readable on a fluorescent-lit hospital monitor.
export const TYPE = {
  caption: { fontSize: 11, fontWeight: 500 as const, lineHeight: 1.45 },
  body: { fontSize: 13, fontWeight: 400 as const, lineHeight: 1.55 },
  label: { fontSize: 12, fontWeight: 600 as const, lineHeight: 1.4, letterSpacing: '0.02em' },
  heading: { fontSize: 14, fontWeight: 600 as const, lineHeight: 1.4 },
};

// P1-P3 = RED (immediate / sepsis / critical lab),
// P4-P7 = AMB (critical vital, AMS, pain, abnormal lab),
// P8+   = NEU (incl. P9 code-status which renderers re-skin red contextually).
export function tierColor(level: string | number): ColorToken {
  const n = typeof level === 'number' ? level : parseInt(String(level).replace(/^P/i, ''), 10);
  if (!Number.isFinite(n)) return NEU;
  if (n <= 3) return RED;
  if (n <= 7) return AMB;
  return NEU;
}
