import { useEffect, type ReactElement } from 'react';
import { SURFACE } from '../styles/tokens';

/**
 * Snippet shape consumed by the modal. Mirrors the `guideline:*` arm of
 * {@link import('../api').CitationIndexEntry} but kept intentionally local —
 * the modal does not need the discriminator, only the renderable fields.
 */
export interface GuidelineSnippetModalData {
  chunk_id: string;
  document_title: string | null;
  section: string | null;
  page_number: number | null;
  content: string | null;
}

interface Props {
  snippet: GuidelineSnippetModalData;
  onClose: () => void;
}

/**
 * Drill-in view for a `guideline:*` citation chip. Renders the full chunk
 * content (no 200-char truncation — that's the chip-tooltip path), document
 * title, section, and page. Click backdrop or press ESC to close.
 *
 * Sits on top of the chat surface as a fixed overlay (parallel to
 * DocumentViewer for fact:obs / fact:intake). No portal — z-index alone is
 * enough because no surface above the chat list uses zIndex >= 1100.
 */
export default function GuidelineSnippetModal({ snippet, onClose }: Props): ReactElement {
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const headerBits: string[] = [];
  if (snippet.section) headerBits.push(snippet.section);
  if (snippet.page_number !== null) headerBits.push(`p.${snippet.page_number}`);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Guideline snippet"
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(15, 23, 42, 0.45)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 1100,
        padding: 16,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: SURFACE.bg,
          border: `1px solid ${SURFACE.border}`,
          borderRadius: 10,
          boxShadow: '0 12px 40px rgba(15, 23, 42, 0.18)',
          width: 'min(560px, 100%)',
          maxHeight: 'calc(100vh - 32px)',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'flex-start',
            justifyContent: 'space-between',
            gap: 12,
            padding: '12px 16px',
            borderBottom: `1px solid ${SURFACE.border}`,
            background: SURFACE.panel,
          }}
        >
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: SURFACE.fgStrong, lineHeight: 1.35 }}>
              {snippet.document_title || 'Guideline snippet'}
            </div>
            {headerBits.length > 0 && (
              <div style={{ fontSize: 12, color: SURFACE.muted, marginTop: 2 }}>
                {headerBits.join(' · ')}
              </div>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close guideline snippet"
            style={{
              border: `1px solid ${SURFACE.border}`,
              background: SURFACE.bg,
              color: SURFACE.fg,
              borderRadius: 6,
              padding: '4px 10px',
              cursor: 'pointer',
              fontSize: 12,
              lineHeight: 1.2,
              flexShrink: 0,
            }}
          >
            Close
          </button>
        </div>
        <div
          style={{
            padding: '14px 16px',
            overflowY: 'auto',
            fontSize: 13,
            lineHeight: 1.45,
            color: SURFACE.fg,
            whiteSpace: 'pre-wrap',
          }}
        >
          {snippet.content && snippet.content.trim().length > 0
            ? snippet.content
            : <span style={{ color: SURFACE.muted, fontStyle: 'italic' }}>No content available for this snippet.</span>
          }
        </div>
        <div
          style={{
            padding: '8px 16px',
            borderTop: `1px solid ${SURFACE.border}`,
            background: SURFACE.panel,
            fontSize: 11,
            color: SURFACE.muted,
          }}
        >
          chunk_id: <code style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>{snippet.chunk_id}</code>
        </div>
      </div>
    </div>
  );
}
