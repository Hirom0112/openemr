import type React from 'react';

export const RED = { bg: '#FCEBEB', border: '#E24B4A', text: '#7F1D1D', secondary: '#991B1B' };
export const AMB = { bg: '#FAEEDA', border: '#EF9F27', text: '#78350F', secondary: '#92400E' };
export const NEU = { bg: '#F8F9FA', border: '#E5E7EB', text: '#374151', secondary: '#6B7280' };
export const GREEN_PILL = { bg: '#DCFCE7', border: '#86EFAC', text: '#166534' };
export const MUTED = '#9CA3AF';
export const LINK_TINT = '#E8EDF8';

export type ColorToken = typeof RED;

export function claimRowStyle(color: ColorToken): React.CSSProperties {
  return {
    background: color.bg,
    borderLeft: `3px solid ${color.border}`,
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
  return { textTransform: 'uppercase', fontSize: 11, fontWeight: 500, letterSpacing: '0.06em', color: color.text };
}

// Census visual primitives (extracted from CensusRenderer.tsx)
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
    borderLeft: `3px solid ${color.border}`,
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

// Severity scale used for inline severity dots and row treatments.
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
      return { dot: MUTED, text: NEU.text, accent: NEU.bg };
  }
}

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
