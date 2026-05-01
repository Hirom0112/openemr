import { useState } from 'react';
import type { Citation } from '../types';

interface DisclaimerIconProps {
  citations?: Citation[];
  date?: string | Date;
}

function mostRecentDate(citations: Citation[]): string | null {
  const dates = citations
    .map((c) => c.effective_datetime)
    .filter((d): d is string => !!d)
    .map((d) => new Date(d).getTime())
    .filter((t) => !isNaN(t));
  if (dates.length === 0) return null;
  return new Date(Math.max(...dates)).toISOString().slice(0, 10);
}

function formatDate(d: string | Date): string {
  const dt = typeof d === 'string' ? new Date(d) : d;
  return dt.toISOString().slice(0, 10);
}

export default function DisclaimerIcon({ citations, date }: DisclaimerIconProps) {
  const [visible, setVisible] = useState(false);

  const dateStr =
    (citations && mostRecentDate(citations)) ??
    (date ? formatDate(date) : new Date().toISOString().slice(0, 10));

  const text = `This summary is generated from EHR data as of ${dateStr}. Verify critical values directly in the chart.`;

  return (
    <div
      style={{ position: 'relative', display: 'inline-flex', alignItems: 'center' }}
      onMouseEnter={() => setVisible(true)}
      onMouseLeave={() => setVisible(false)}
    >
      <span
        aria-label={text}
        role="img"
        style={{
          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          width: 16, height: 16, borderRadius: '50%',
          border: '1.5px solid #9ca3af', color: '#9ca3af',
          fontSize: 10, fontWeight: 500, cursor: 'default',
          userSelect: 'none', flexShrink: 0,
        }}
      >
        i
      </span>

      {visible && (
        <div style={{
          position: 'absolute', bottom: 'calc(100% + 6px)', left: '50%',
          transform: 'translateX(-50%)',
          background: '#1f2937', color: '#f9fafb',
          fontSize: 11, lineHeight: 1.5,
          padding: '6px 10px', borderRadius: 6,
          width: 240, whiteSpace: 'normal',
          pointerEvents: 'none', zIndex: 50,
        }}>
          {text}
          <div style={{
            position: 'absolute', top: '100%', left: '50%',
            transform: 'translateX(-50%)',
            width: 0, height: 0,
            borderLeft: '5px solid transparent',
            borderRight: '5px solid transparent',
            borderTop: '5px solid #1f2937',
          }} />
        </div>
      )}
    </div>
  );
}
