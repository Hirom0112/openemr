import type React from 'react';
import { sectionHeadingStyle, claimRowStyle, footerDividerStyle } from '../../styles/tokens';
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
        {subtitle && <div style={{ fontSize: 12, color: '#6B7280', marginTop: 1 }}>{subtitle}</div>}
      </div>
      {pill}
    </div>
  );
}

export function FooterDivider({ children }: { children: React.ReactNode }) {
  return <div style={footerDividerStyle()}>{children}</div>;
}
