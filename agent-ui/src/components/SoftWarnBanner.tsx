import type { ReactElement } from 'react';
import type { SoftWarn } from '../types/citation';
import { AMB } from '../styles/tokens';

/**
 * Yellow banner stack for soft warnings emitted by extractors
 * (e.g. low OCR confidence, missing-but-non-blocking field).
 *
 * Renders nothing when `warns` is empty so the parent can mount it
 * unconditionally without inserting a hollow div into the chat surface.
 */

export interface SoftWarnBannerProps {
  warns: SoftWarn[];
}

export default function SoftWarnBanner({ warns }: SoftWarnBannerProps): ReactElement | null {
  if (!warns || warns.length === 0) return null;
  return (
    <div role="alert" style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 10 }}>
      {warns.map((w, i) => (
        <div
          key={`${w.code}-${i}`}
          data-testid="soft-warn-banner"
          style={{
            background: AMB.bg,
            border: `1px solid ${AMB.border}`,
            color: AMB.text,
            borderRadius: 6,
            padding: '6px 10px',
            fontSize: 12,
            display: 'flex',
            alignItems: 'flex-start',
            gap: 8,
          }}
        >
          <span aria-hidden="true" style={{ fontWeight: 700 }}>!</span>
          <div style={{ flex: 1 }}>
            <div>{w.message}</div>
            {w.fields.length > 0 && (
              <div style={{ marginTop: 2, fontSize: 11, opacity: 0.8 }}>
                Fields: {w.fields.join(', ')}
              </div>
            )}
          </div>
          <span style={{ fontSize: 10, opacity: 0.6 }}>{w.code}</span>
        </div>
      ))}
    </div>
  );
}
