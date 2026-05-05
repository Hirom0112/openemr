import { useEffect, useMemo, useRef, useState } from 'react';
import type { ReactElement } from 'react';
import { loadPdf, type PDFDocumentProxy } from '../lib/pdfjs';
import BboxOverlay from './BboxOverlay';
import type { BboxLayoutBlock, Citation } from '../types/citation';
import { SURFACE, BRAND } from '../styles/tokens';

/**
 * pdf.js + bbox overlay viewer.
 *
 * Two ways to drive the active citation:
 *   1) Pass `citations` (array) + `activeIndex` and the parent owns the index
 *      (parent handles arrow-key cycling externally).
 *   2) Pass `citations` (array) ONLY and let this component own the index;
 *      ArrowLeft / ArrowRight then cycle internally. We default to (2) — it
 *      keeps the cycling concern with the surface that owns the keyboard.
 *
 * `pdfUrl` and `pdfBytes` are interchangeable. The component is agnostic about
 * how the parent obtained the bytes (DocumentReference URL, /document/preview
 * blob, file picker — anything goes).
 */

export interface DocumentViewerProps {
  pdfUrl?: string;
  pdfBytes?: ArrayBuffer;
  /** All citations attached to the current fact (sibling sources). */
  citations?: Citation[];
  /** Optional: parent-controlled index. Falls back to internal state. */
  activeIndex?: number;
  /** Optional: notify parent when arrow keys move the index. */
  onActiveIndexChange?: (idx: number) => void;
  /** Layout blocks (one per OCR block) used to resolve bbox by field_or_chunk_id. */
  bboxLayout?: BboxLayoutBlock[];
  onClose?: () => void;
}

interface PageRender {
  width: number;
  height: number;
  pageWidth: number;
  pageHeight: number;
}

function pageNumberFromCitation(c: Citation | undefined): number {
  if (!c) return 1;
  const raw = c.page_or_section;
  if (raw == null) return 1;
  const m = /\d+/.exec(String(raw));
  return m ? Math.max(1, parseInt(m[0], 10)) : 1;
}

export default function DocumentViewer(props: DocumentViewerProps): ReactElement {
  const {
    pdfUrl,
    pdfBytes,
    citations = [],
    activeIndex: controlledIndex,
    onActiveIndexChange,
    bboxLayout = [],
    onClose,
  } = props;

  const [internalIndex, setInternalIndex] = useState(0);
  const activeIndex = controlledIndex ?? internalIndex;
  const activeCitation = citations[activeIndex];

  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [pageInfo, setPageInfo] = useState<PageRender | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(false);

  // Load PDF (URL or bytes). Re-run only when source changes — the proxy is
  // reused across page renders.
  //
  // pdfjs transfers ArrayBuffer ownership to its worker, leaving the
  // original buffer detached. If a parent component holds the same
  // ArrayBuffer in state across multiple opens (chip click → close →
  // chip click), the second loadPdf would fail with "Cannot perform
  // Construct on a detached ArrayBuffer". Clone via slice(0) so pdfjs
  // can transfer the clone and the original stays valid for next time.
  useEffect(() => {
    let cancelled = false;
    let source: string | ArrayBuffer | undefined = undefined;
    if (pdfBytes) {
      try {
        source = pdfBytes.slice(0);
      } catch (err) {
        console.error('[DocumentViewer] ArrayBuffer slice failed (already detached?)', err);
        source = undefined;
      }
    } else if (pdfUrl) {
      source = pdfUrl;
    }
    if (!source) {
      setPdf(null);
      return;
    }
    setLoading(true);
    setLoadError(null);
    loadPdf(source)
      .then((doc) => { if (!cancelled) setPdf(doc); })
      .catch((err: unknown) => {
        if (cancelled) return;
        console.error('[DocumentViewer] PDF load failed', err);
        setLoadError('Could not load document');
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => {
      cancelled = true;
    };
  }, [pdfUrl, pdfBytes]);

  // Render the page indicated by the active citation.
  const targetPage = pageNumberFromCitation(activeCitation);
  useEffect(() => {
    if (!pdf || !canvasRef.current) return;
    let cancelled = false;
    const canvas = canvasRef.current;
    const renderPage = async (): Promise<void> => {
      const safePage = Math.min(Math.max(1, targetPage), pdf.numPages);
      const page = await pdf.getPage(safePage);
      const baseViewport = page.getViewport({ scale: 1 });
      // Fit to ~720px wide for readability; capped at intrinsic width.
      const desiredWidth = Math.min(720, baseViewport.width * 1.5);
      const scale = desiredWidth / baseViewport.width;
      const viewport = page.getViewport({ scale });
      const ctx = canvas.getContext('2d');
      if (!ctx || cancelled) return;
      canvas.width = Math.floor(viewport.width);
      canvas.height = Math.floor(viewport.height);
      await page.render({ canvasContext: ctx, viewport }).promise;
      if (cancelled) return;
      setPageInfo({
        width: canvas.width,
        height: canvas.height,
        pageWidth: baseViewport.width,
        pageHeight: baseViewport.height,
      });
    };
    void renderPage().catch((err: unknown) => {
      if (cancelled) return;
      console.error('[DocumentViewer] page render failed', err);
      setLoadError('Could not render page');
    });
    return () => { cancelled = true; };
  }, [pdf, targetPage]);

  // Resolve bbox for the active citation's field_or_chunk_id, scoped to the page.
  const activeBbox = useMemo<[number, number, number, number] | null>(() => {
    if (!activeCitation) return null;
    const block = bboxLayout.find((b) => b.bbox_id === activeCitation.field_or_chunk_id);
    if (!block) return null;
    return block.bbox;
  }, [activeCitation, bboxLayout]);

  // Keyboard cycling. Only attaches when we have multiple citations.
  useEffect(() => {
    if (citations.length <= 1) return;
    const handler = (e: KeyboardEvent): void => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight' && e.key !== 'Escape') return;
      if (e.key === 'Escape') {
        onClose?.();
        return;
      }
      e.preventDefault();
      const delta = e.key === 'ArrowRight' ? 1 : -1;
      const next = (activeIndex + delta + citations.length) % citations.length;
      if (controlledIndex == null) setInternalIndex(next);
      onActiveIndexChange?.(next);
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [citations.length, activeIndex, controlledIndex, onActiveIndexChange, onClose]);

  const hasSource = !!(pdfUrl || pdfBytes);

  return (
    <div
      ref={containerRef}
      role="dialog"
      aria-modal="true"
      aria-label="Document viewer"
      style={{
        position: 'fixed',
        top: 0,
        right: 0,
        width: 'min(820px, 95vw)',
        height: '100vh',
        background: SURFACE.bg,
        borderLeft: `1px solid ${SURFACE.border}`,
        boxShadow: '-4px 0 16px rgba(15, 23, 42, 0.16)',
        display: 'flex',
        flexDirection: 'column',
        zIndex: 1000,
      }}
    >
      <header
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '10px 14px',
          borderBottom: `1px solid ${SURFACE.border}`,
          background: SURFACE.panel,
          fontSize: 12,
          color: SURFACE.fgStrong,
        }}
      >
        <span style={{ color: BRAND.base, fontWeight: 600 }}>Document</span>
        {activeCitation && (
          <span style={{ color: SURFACE.muted }}>
            {activeCitation.source_id} · p{targetPage}
            {citations.length > 1 ? ` · ${activeIndex + 1}/${citations.length}` : ''}
          </span>
        )}
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
          {citations.length > 1 && (
            <span aria-hidden="true" style={{ fontSize: 10, color: SURFACE.muted }}>
              ← / → to cycle
            </span>
          )}
          <button
            type="button"
            aria-label="Close document viewer"
            onClick={onClose}
            style={{
              background: 'transparent',
              border: `1px solid ${SURFACE.border}`,
              color: SURFACE.fgStrong,
              borderRadius: 6,
              padding: '2px 8px',
              cursor: 'pointer',
              fontFamily: 'inherit',
              fontSize: 12,
              minHeight: 26,
            }}
          >
            ✕
          </button>
        </span>
      </header>

      <div
        style={{
          flex: 1,
          minHeight: 0,
          overflow: 'auto',
          padding: 16,
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'flex-start',
          background: '#1f2937',
        }}
      >
        {!hasSource && (
          <div style={{ color: '#e5e7eb', fontSize: 13, marginTop: 24 }}>No document attached.</div>
        )}
        {hasSource && loadError && (
          <div role="alert" style={{ color: '#fecaca', fontSize: 13, marginTop: 24 }}>
            {loadError}
          </div>
        )}
        {hasSource && !loadError && loading && !pdf && (
          <div style={{ color: '#e5e7eb', fontSize: 13, marginTop: 24 }}>Loading document…</div>
        )}
        {hasSource && !loadError && (
          <div style={{ position: 'relative', display: 'inline-block' }}>
            <canvas
              ref={canvasRef}
              data-testid="pdf-canvas"
              style={{ display: 'block', boxShadow: '0 2px 8px rgba(0,0,0,0.4)', background: 'white' }}
            />
            {pageInfo && (
              <BboxOverlay
                canvasWidth={pageInfo.width}
                canvasHeight={pageInfo.height}
                pdfPageWidth={pageInfo.pageWidth}
                pdfPageHeight={pageInfo.pageHeight}
                bbox={activeBbox}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
