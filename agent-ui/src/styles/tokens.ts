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
  return { background: '#E8EDF8', color: '#374151', border: '1px solid #E5E7EB' };
}

export function footerDividerStyle(): React.CSSProperties {
  return { borderTop: '1px solid #E5E7EB', marginTop: 16, paddingTop: 10, fontSize: 11, color: '#9CA3AF' };
}

export function sectionHeadingStyle(color: ColorToken): React.CSSProperties {
  return { textTransform: 'uppercase', fontSize: 11, fontWeight: 500, letterSpacing: '0.06em', color: color.text };
}
