import type React from 'react';
import {
  sectionHeadingStyle,
  claimRowStyle,
  footerDividerStyle,
  metricCardStyle,
  patientRowStyle,
  livePillStyle,
  admitBadgeStyle,
  NEU,
  MUTED,
} from '../../styles/tokens';
import type { ColorToken } from '../../styles/tokens';

export function SectionHeading({ color, children }: { color: ColorToken; children: React.ReactNode }) {
  return <div style={{ ...sectionHeadingStyle(color), marginBottom: 6, marginTop: 14 }}>{children}</div>;
}

export function ClaimRow({ color, children }: { color: ColorToken; children: React.ReactNode }) {
  return <div style={{ ...claimRowStyle(color), fontSize: 13, color: color.text }}>{children}</div>;
}

export function Pill({ color, label }: { color: { bg: string; border: string; text: string }; label: string }) {
  return (
    <span
      style={{
        background: color.bg,
        border: `1px solid ${color.border}`,
        color: color.text,
        borderRadius: 10,
        padding: '2px 8px',
        fontSize: 11,
      }}
    >
      {label}
    </span>
  );
}

export function Header({
  title,
  subtitle,
  pill,
}: {
  title: string;
  subtitle?: string;
  pill?: React.ReactNode;
}) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 10 }}>
      <div>
        <div style={{ fontSize: 15, fontWeight: 500, color: '#111' }}>{title}</div>
        {subtitle && <div style={{ fontSize: 12, color: NEU.secondary, marginTop: 1 }}>{subtitle}</div>}
      </div>
      {pill}
    </div>
  );
}

export function FooterDivider({ children }: { children: React.ReactNode }) {
  return <div style={footerDividerStyle()}>{children}</div>;
}

// 8px tier dot used in census section headings.
export function TierDot({ color }: { color: ColorToken }) {
  return (
    <span
      aria-hidden="true"
      style={{ width: 8, height: 8, borderRadius: '50%', background: color.border, flexShrink: 0, display: 'inline-block' }}
    />
  );
}

// Single metric card from the census summary strip.
export function MetricCard({ label, count, color }: { label: string; count: number; color: ColorToken }) {
  return (
    <li style={metricCardStyle(color)}>
      <div style={{ fontSize: 11, fontWeight: 500, color: color.text, marginBottom: 2 }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 500, color: color.text }}>{count}</div>
    </li>
  );
}

// 4-up CSS grid container of MetricCards.
export function MetricStrip({
  items,
  ariaLabel,
}: {
  items: { label: string; count: number; color: ColorToken }[];
  ariaLabel?: string;
}) {
  return (
    <ul
      role="list"
      aria-label={ariaLabel}
      style={{
        display: 'grid',
        gridTemplateColumns: `repeat(${Math.max(items.length, 1)}, 1fr)`,
        gap: 6,
        listStyle: 'none',
        padding: 0,
        margin: '0 0 4px',
      }}
    >
      {items.map((it) => (
        <MetricCard key={it.label} label={it.label} count={it.count} color={it.color} />
      ))}
    </ul>
  );
}

// Generic patient row matching the census shape.
export function PatientRow({
  left,
  title,
  subtitle,
  badges,
  actions,
  color,
}: {
  left?: React.ReactNode;
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  badges?: React.ReactNode;
  actions?: React.ReactNode;
  color: ColorToken;
}) {
  return (
    <div style={patientRowStyle(color)}>
      {left !== undefined && (
        <span style={{ fontSize: 11, fontWeight: 500, color: color.text, flexShrink: 0, minWidth: 22 }}>{left}</span>
      )}
      <span style={{ flex: 1, minWidth: 0 }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 13, fontWeight: 500, color: color.text }}>{title}</span>
          {badges}
        </span>
        {subtitle !== undefined && (
          <span
            style={{
              display: 'block',
              fontSize: 12,
              color: color.secondary,
              marginTop: 1,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {subtitle}
          </span>
        )}
      </span>
      {actions}
    </div>
  );
}

export function LivePill() {
  return (
    <span style={livePillStyle()}>
      <span aria-hidden="true" style={{ width: 6, height: 6, borderRadius: '50%', background: '#16a34a' }} />
      Live
    </span>
  );
}

// Admit-day badge: "Overnight" on day 0, "Day N+1" otherwise.
export function AdmitBadge({ days }: { days: number | undefined | null }) {
  if (days === undefined || days === null) return null;
  const badge =
    days === 0
      ? { label: 'Overnight', bg: '#e0f2fe', fg: '#0369a1', border: '#7dd3fc' }
      : { label: `Day ${days + 1}`, bg: '#f3f4f6', fg: '#6b7280', border: '#d1d5db' };
  return <span style={admitBadgeStyle(badge)}>{badge.label}</span>;
}

// Right-aligned disclaimer footer used at the bottom of each renderer.
export function DisclaimerFooter({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        marginTop: 14,
        paddingTop: 10,
        borderTop: `1px solid ${NEU.border}`,
        display: 'flex',
        justifyContent: 'flex-end',
      }}
    >
      {children}
    </div>
  );
}

// Re-export MUTED to avoid double-import dance in renderers.
export { MUTED };
