import { useState, useEffect, type ReactNode } from 'react';
import type React from 'react';
import ReactMarkdown from 'react-markdown';
import {
  sectionHeadingStyle,
  claimRowStyle,
  footerDividerStyle,
  metricCardStyle,
  patientRowStyle,
  livePillStyle,
  admitBadgeStyle,
  severityColor,
  tierChipStyle,
  NEU,
  MUTED,
  SURFACE,
  TYPE,
} from '../../styles/tokens';
import type { ColorToken, Severity } from '../../styles/tokens';
import type { Citation } from '../../types';

export function SectionHeading({ color, children }: { color: ColorToken; children: React.ReactNode }) {
  return <div style={{ ...sectionHeadingStyle(color), marginBottom: 8, marginTop: 16 }}>{children}</div>;
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
  meta,
}: {
  title: string;
  subtitle?: string;
  pill?: React.ReactNode;
  meta?: ReactNode;
}) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
      <div>
        <div style={{ fontSize: 16, fontWeight: 600, color: SURFACE.fgStrong, letterSpacing: '-0.01em', lineHeight: 1.3 }}>{title}</div>
        {subtitle && <div style={{ ...TYPE.body, fontSize: 12, color: SURFACE.muted, marginTop: 2 }}>{subtitle}</div>}
      </div>
      {(pill || meta) && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {meta}
          {pill}
        </div>
      )}
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

// Single metric card from the census summary strip. Big number, small muted
// label below — scannable at a glance during rounds. The severity tint is
// carried by metricCardStyle() so the row reads as a colored band.
export function MetricCard({ label, count, color }: { label: string; count: number; color: ColorToken }) {
  return (
    <li
      style={{
        ...metricCardStyle(color),
        padding: '8px 6px 7px',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      <div
        style={{
          fontSize: 18,
          fontWeight: 700,
          lineHeight: 1.1,
          color: color === NEU ? SURFACE.fgStrong : color.text,
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        {count}
      </div>
      <div
        style={{
          ...TYPE.caption,
          fontWeight: 500,
          color: color === NEU ? SURFACE.muted : color.secondary,
          marginTop: 3,
          textAlign: 'center',
        }}
      >
        {label}
      </div>
    </li>
  );
}

// CSS-grid container of MetricCards. Tighter gap than before — the row now
// reads as a single cohesive metric strip rather than separated tiles.
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
        gap: 4,
        listStyle: 'none',
        padding: 0,
        margin: '0 0 8px',
      }}
    >
      {items.map((it) => (
        <MetricCard key={it.label} label={it.label} count={it.count} color={it.color} />
      ))}
    </ul>
  );
}

// Tier chip ("P2", "P3") for the leading edge of a patient row.
export function TierChip({ level, color }: { level: number | string; color: ColorToken }) {
  return <span style={tierChipStyle(color)}>{`P${level}`}</span>;
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
    <div
      style={{
        ...patientRowStyle(color),
        padding: '10px 12px',
        gap: 12,
        marginBottom: 4,
      }}
    >
      {left !== undefined && (
        typeof left === 'string' || typeof left === 'number'
          ? <span style={{ fontSize: 11, fontWeight: 600, color: color.text, flexShrink: 0, minWidth: 22 }}>{left}</span>
          : left
      )}
      <span style={{ flex: 1, minWidth: 0 }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <span
            style={{
              fontSize: 13,
              fontWeight: 600,
              color: color === NEU ? SURFACE.fg : color.text,
              letterSpacing: '-0.005em',
            }}
          >
            {title}
          </span>
          {badges}
        </span>
        {subtitle !== undefined && (
          <span
            style={{
              display: 'block',
              ...TYPE.body,
              fontSize: 12,
              color: color === NEU ? SURFACE.muted : color.secondary,
              marginTop: 2,
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

// ── Severity dot ─────────────────────────────────────────────────────────────
export function SeverityDot({ level }: { level: Severity }) {
  return (
    <span
      aria-hidden="true"
      style={{
        width: 8,
        height: 8,
        borderRadius: '50%',
        background: severityColor(level).dot,
        display: 'inline-block',
        verticalAlign: 'middle',
        flexShrink: 0,
      }}
    />
  );
}

// ── Metadata line (◆ Label · HH:MM · N sources) ─────────────────────────────
export function MetadataLine({
  icon,
  label,
  timestamp,
  sources,
}: {
  icon?: ReactNode;
  label: string;
  timestamp?: string;
  sources?: number;
}) {
  const parts: ReactNode[] = [];
  parts.push(<span key="label">{label}</span>);
  if (timestamp) parts.push(<span key="t">{timestamp}</span>);
  if (typeof sources === 'number' && sources > 0) {
    parts.push(<span key="s">{`${sources} source${sources === 1 ? '' : 's'}`}</span>);
  }
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 6,
        fontSize: 11,
        color: MUTED,
        marginBottom: 4,
      }}
    >
      {icon && <span aria-hidden="true">{icon}</span>}
      {parts.map((p, i) => (
        <span key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          {i > 0 && <span aria-hidden="true">·</span>}
          {p}
        </span>
      ))}
    </div>
  );
}

// ── Citation footer (▾/▸ N sources) ─────────────────────────────────────────
export function CitationFooter({
  count,
  onToggle,
  expanded,
  children,
}: {
  count: number;
  onToggle?: () => void;
  expanded?: boolean;
  children?: ReactNode;
}) {
  // Self-managed expand state when no controlled props provided.
  const [internalOpen, setInternalOpen] = useState(false);
  if (count <= 0) return null;
  const isControlled = typeof expanded === 'boolean';
  const open = isControlled ? !!expanded : internalOpen;
  const toggle = () => {
    if (onToggle) onToggle();
    if (!isControlled) setInternalOpen((v) => !v);
  };
  return (
    <div
      style={{
        marginTop: 12,
        paddingTop: 8,
        borderTop: `1px solid ${NEU.border}`,
      }}
    >
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        style={{
          background: 'none',
          border: 'none',
          padding: 0,
          margin: 0,
          fontSize: 11,
          color: MUTED,
          cursor: 'pointer',
          display: 'inline-flex',
          alignItems: 'center',
          gap: 6,
          fontFamily: 'inherit',
        }}
      >
        <span aria-hidden="true">{open ? '▾' : '▸'}</span>
        {`${count} source${count === 1 ? '' : 's'}`}
      </button>
      {open && children && (
        <div style={{ marginTop: 6, fontSize: 12, color: NEU.text }}>{children}</div>
      )}
    </div>
  );
}

// ── Skeleton row (animated shimmer) ─────────────────────────────────────────
let __skeletonInjected = false;
function injectSkeletonKeyframes() {
  if (__skeletonInjected || typeof document === 'undefined') return;
  __skeletonInjected = true;
  const style = document.createElement('style');
  style.textContent = `@keyframes copilot-shimmer {
    0% { background-position: -200px 0; }
    100% { background-position: calc(200px + 100%) 0; }
  }`;
  document.head.appendChild(style);
}

export function SkeletonRow({ width, height }: { width?: number | string; height?: number | string }) {
  useEffect(() => {
    injectSkeletonKeyframes();
  }, []);
  return (
    <div
      aria-hidden="true"
      style={{
        width: width ?? '100%',
        height: height ?? 12,
        borderRadius: 4,
        background: `linear-gradient(90deg, ${NEU.bg} 0px, #eef0f2 40px, ${NEU.bg} 80px)`,
        backgroundSize: '200px 100%',
        animation: 'copilot-shimmer 1.4s ease-in-out infinite',
        marginBottom: 6,
      }}
    />
  );
}

/**
 * Insert newlines so single-line LLM tables parse as GFM tables.
 *
 * The LLM occasionally collapses a markdown table onto one line:
 *   "| Category | Detail | |---|---| | row | val | | row2 | val2 |"
 * react-markdown's table parser needs each row on its own line. Detect
 * the table-start signature ("| header | ... | |---|---| ...") and split
 * on " | " boundaries that follow each "|" cell-close.
 */
export function repairInlineMarkdownTables(input: string): string {
  // Cheap pre-check — if there's no "|---|" at all, there are no tables.
  if (!input.includes('|---|') && !/\|\s*-+\s*\|/.test(input)) return input;
  // Walk each line; only rewrite lines that have a separator AND no \n
  // inside the table region.
  return input
    .split('\n')
    .map((line) => {
      if (!/\|\s*-+/.test(line)) return line;
      // Split on " | |" boundaries (cell-close + cell-open across rows),
      // also handle "---| |" (separator -> next row). Keep the trailing
      // pipe on each emitted row so GFM still parses.
      // Strategy: insert \n before any "| " that follows a "| " preceded
      // by another "|" — the "| |" sequence between rows.
      return line.replace(/\|\s+\|/g, '|\n|').replace(/\|\s*$/g, '|');
    })
    .join('\n');
}

// ── Markdown narrative renderer ─────────────────────────────────────────────
// Renders narrative text (model output that may include `##`, `**`, lists,
// inline code) safely. Default escaping is on — no `rehype-raw`. Long URLs
// and code blocks wrap so the iframe never gets a horizontal scrollbar.
//
// `citations` is accepted for forward-compat (chip-anchor wiring), but
// not required for the safe-render fix.
export function Markdown({
  narrative,
  citations: _citations,
}: {
  narrative: string;
  citations?: Citation[];
}) {
  void _citations;
  if (!narrative) return null;
  // Repair LLM-produced markdown tables that were emitted on a single line
  // (e.g. "| Category | Detail | |---|---| | row | val |" with no \n
  // between rows). react-markdown's GFM table parser requires each row on
  // its own line; without this fix the renderer leaves the raw pipes in
  // the page and the table never materializes.
  const cleaned = repairInlineMarkdownTables(narrative);
  return (
    <div
      style={{
        fontSize: 13,
        color: NEU.text,
        lineHeight: 1.6,
        wordBreak: 'break-word',
        overflowWrap: 'anywhere',
      }}
      className="copilot-markdown"
    >
      <ReactMarkdown
        components={{
          p: ({ children }) => <p style={{ margin: '0 0 8px' }}>{children}</p>,
          ul: ({ children }) => (
            <ul style={{ margin: '0 0 8px', paddingLeft: 18 }}>{children}</ul>
          ),
          ol: ({ children }) => (
            <ol style={{ margin: '0 0 8px', paddingLeft: 18 }}>{children}</ol>
          ),
          li: ({ children }) => <li style={{ marginBottom: 2 }}>{children}</li>,
          h1: ({ children }) => (
            <div style={{ fontSize: 14, fontWeight: 600, margin: '8px 0 6px' }}>{children}</div>
          ),
          h2: ({ children }) => (
            <div style={{ fontSize: 13, fontWeight: 600, margin: '8px 0 6px' }}>{children}</div>
          ),
          h3: ({ children }) => (
            <div style={{ fontSize: 13, fontWeight: 600, margin: '6px 0 4px' }}>{children}</div>
          ),
          code: ({ children }) => (
            <code
              style={{
                background: NEU.bg,
                border: `1px solid ${NEU.border}`,
                borderRadius: 3,
                padding: '0 4px',
                fontSize: 12,
                fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                wordBreak: 'break-word',
              }}
            >
              {children}
            </code>
          ),
          pre: ({ children }) => (
            <pre
              style={{
                background: NEU.bg,
                border: `1px solid ${NEU.border}`,
                borderRadius: 4,
                padding: 8,
                fontSize: 12,
                overflowX: 'auto',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                margin: '0 0 8px',
              }}
            >
              {children}
            </pre>
          ),
          a: ({ href, children }) => (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              style={{ color: '#2c3e9e', wordBreak: 'break-all' }}
            >
              {children}
            </a>
          ),
          strong: ({ children }) => (
            <strong style={{ fontWeight: 600 }}>{children}</strong>
          ),
        }}
      >
        {cleaned}
      </ReactMarkdown>
    </div>
  );
}
