/**
 * Phase-3 Documents tab — two-column rich review panel.
 *
 * Replaces ApprovalModal for PDF / PNG / JPEG / DOCX rows whose
 * target_resource_type is one of {Observation, IntakeFormField}. HL7/XLSX/TIFF
 * rows continue to use ApprovalModal — those formats lack the per-field
 * structure that justifies the rich editor.
 *
 * Layout:
 *   ┌─────────────────────────────────────────────┐
 *   │ Header: title · doc id chip · facts pill    │
 *   ├──────────────────────┬──────────────────────┤
 *   │ Source viewer        │ Editor cards         │
 *   │ (left rail, white)   │ (right rail, panel)  │
 *   ├──────────────────────┴──────────────────────┤
 *   │ Footer: status · Cancel · Save & Approve    │
 *   └─────────────────────────────────────────────┘
 *
 * v1.5:
 *   - Source preview now actually renders. PDF → pdf.js via DocumentViewer.
 *     PNG / JPEG → <img> with citation chip strip across the top. DOCX falls
 *     back to the synthesized-paragraph viewer because we don't (yet) parse
 *     DOCX bytes client-side; the citation quotes are still surfaced.
 *   - Citation chip strip across the top of the left rail lets the operator
 *     pick a citation directly; "Source →" buttons in the editor cards drive
 *     the same activeIndex.
 *   - Loading + error states for the binary fetch.
 */

import { useCallback, useEffect, useMemo, useState, type ReactElement } from 'react';
import {
  approveOne,
  fetchDocumentBinary,
  fetchPostApprovalContext,
  getPendingOne,
  rejectOne,
  type PendingExtractionRow,
  type PostApprovalContext,
} from '../api';
import DocumentViewer from './DocumentViewer';
import DocxParagraphsViewer from './DocxParagraphsViewer';
import { resolveEditor, type FieldEditorProps } from './FieldEditors';
import { BRAND, NEU, RED, SURFACE } from '../styles/tokens';
import type { Citation, BboxLayoutBlock } from '../types/citation';

export interface DocumentReviewPanelProps {
  baseUrl: string;
  patientId: string;
  documentReferenceId: string;
  fileBatchId: string;
  pendingRowIds: number[];
  onClose: () => void;
  onCompleted: (
    documentReferenceId: string,
    ragResult: PostApprovalContext | null,
  ) => void;
}

interface RowState {
  decision: 'pending' | 'approved' | 'rejected';
  override: Record<string, unknown> | null;
  rejectReason: string | null;
  status: 'idle' | 'busy' | 'done' | 'error';
  error?: string;
}

interface LoadedRow {
  row: PendingExtractionRow;
  state: RowState;
}

type ViewerKind = 'pdf' | 'image' | 'docx' | 'unknown';

const ROW_LIMIT = 50;

const PDF_MIME = 'application/pdf';
const PNG_MIME = 'image/png';
const JPEG_MIME = 'image/jpeg';
const DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';

function _initialState(row: PendingExtractionRow): RowState {
  if (row.state === 'written' || row.state === 'approved') {
    return { decision: 'approved', override: null, rejectReason: null, status: 'done' };
  }
  if (row.state === 'rejected') {
    return { decision: 'rejected', override: null, rejectReason: null, status: 'done' };
  }
  return { decision: 'pending', override: null, rejectReason: null, status: 'idle' };
}

/** Pull every citation off every row's payload, in row order. */
function _collectCitations(rows: LoadedRow[]): Citation[] {
  const out: Citation[] = [];
  for (const r of rows) {
    const payload = r.row.payload as Record<string, unknown>;
    const value =
      payload.value && typeof payload.value === 'object' && !Array.isArray(payload.value)
        ? (payload.value as Record<string, unknown>)
        : payload;
    const candidates = Array.isArray(value.citations)
      ? value.citations
      : Array.isArray(payload.citations)
        ? payload.citations
        : [];
    for (const c of candidates) {
      if (!c || typeof c !== 'object') continue;
      const obj = c as Record<string, unknown>;
      const sourceId = typeof obj.source_id === 'string' ? obj.source_id : null;
      const fieldId =
        (typeof obj.field_or_chunk_id === 'string' && obj.field_or_chunk_id) ||
        (typeof obj.field_id === 'string' ? obj.field_id : null);
      const quote =
        (typeof obj.quote_or_value === 'string' && obj.quote_or_value) ||
        (typeof obj.quote === 'string' && obj.quote) ||
        (typeof obj.text === 'string' ? obj.text : '');
      const page = typeof obj.page === 'number' ? obj.page : undefined;
      const pageOrSection =
        typeof obj.page_or_section === 'string' ? obj.page_or_section : null;
      const bbox =
        Array.isArray(obj.bbox) && obj.bbox.length === 4
          ? (obj.bbox as [number, number, number, number])
          : null;
      out.push({
        source_type: 'document',
        source_id: sourceId ?? r.row.document_reference_id,
        page_or_section: pageOrSection,
        field_or_chunk_id: fieldId ?? '',
        quote_or_value: quote || '',
        bbox,
        page: page ?? null,
      });
    }
  }
  return out;
}

/** Synthesize paragraphs for the DOCX fallback (no client-side .docx parse). */
function _synthesizeParagraphs(rows: LoadedRow[]): string[] {
  const out: string[] = [];
  for (const r of rows) {
    const payload = r.row.payload as Record<string, unknown>;
    const value =
      payload.value && typeof payload.value === 'object' && !Array.isArray(payload.value)
        ? (payload.value as Record<string, unknown>)
        : payload;
    const citations = Array.isArray(value.citations)
      ? value.citations
      : Array.isArray(payload.citations)
        ? payload.citations
        : [];
    for (const c of citations) {
      if (c && typeof c === 'object') {
        const q =
          (c as Record<string, unknown>).quote_or_value ??
          (c as Record<string, unknown>).quote ??
          (c as Record<string, unknown>).text;
        if (typeof q === 'string' && q.trim()) {
          out.push(q.trim());
        }
      }
    }
  }
  return out;
}

function _detectKind(contentType: string | null): ViewerKind {
  if (!contentType) return 'unknown';
  const t = contentType.toLowerCase();
  if (t.includes(PDF_MIME)) return 'pdf';
  if (t.includes(PNG_MIME) || t.includes(JPEG_MIME) || t.includes('image/')) return 'image';
  if (t.includes(DOCX_MIME) || t.includes('officedocument.wordprocessingml')) return 'docx';
  return 'unknown';
}

export default function DocumentReviewPanel(props: DocumentReviewPanelProps): ReactElement {
  const {
    baseUrl,
    patientId,
    documentReferenceId,
    fileBatchId,
    pendingRowIds,
    onClose,
    onCompleted,
  } = props;

  const [rows, setRows] = useState<LoadedRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkSummary, setBulkSummary] = useState<string | null>(null);

  // Source preview state
  const [previewBytes, setPreviewBytes] = useState<ArrayBuffer | null>(null);
  const [previewKind, setPreviewKind] = useState<ViewerKind>('unknown');
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [imageBlobUrl, setImageBlobUrl] = useState<string | null>(null);

  // Active citation index drives the viewer (PDF + image branches).
  const [activeCitationIndex, setActiveCitationIndex] = useState(0);

  // Load rows once.
  useEffect(() => {
    let cancelled = false;
    const ids = pendingRowIds.slice(0, ROW_LIMIT);
    if (ids.length === 0) return;
    setLoading(true);
    setLoadError(null);
    void (async () => {
      try {
        const fetched = await Promise.all(
          ids.map((id) => getPendingOne(baseUrl, id).catch((err) => ({ _err: err, _id: id }))),
        );
        if (cancelled) return;
        const loaded: LoadedRow[] = [];
        for (const r of fetched) {
          if (r && typeof r === 'object' && '_err' in r) continue;
          const row = r as PendingExtractionRow;
          loaded.push({ row, state: _initialState(row) });
        }
        setRows(loaded);
      } catch (err) {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : 'Failed to load rows.');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [baseUrl, pendingRowIds]);

  // Fetch the source preview bytes once we know the documentReferenceId.
  useEffect(() => {
    let cancelled = false;
    if (!documentReferenceId) return;
    setPreviewLoading(true);
    setPreviewError(null);
    setPreviewBytes(null);
    setPreviewKind('unknown');
    void (async () => {
      try {
        const { bytes, contentType } = await fetchDocumentBinary(baseUrl, documentReferenceId);
        if (cancelled) return;
        setPreviewBytes(bytes);
        setPreviewKind(_detectKind(contentType));
      } catch (err) {
        if (cancelled) return;
        setPreviewError(err instanceof Error ? err.message : 'Failed to load preview.');
      } finally {
        if (!cancelled) setPreviewLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [baseUrl, documentReferenceId]);

  // Build / revoke image blob URL when bytes land for an image preview.
  useEffect(() => {
    if (previewKind !== 'image' || !previewBytes) {
      setImageBlobUrl(null);
      return;
    }
    const blob = new Blob([previewBytes.slice(0)], { type: 'image/*' });
    const url = URL.createObjectURL(blob);
    setImageBlobUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [previewBytes, previewKind]);

  const setRowState = useCallback((rowId: number, patch: Partial<RowState>) => {
    setRows((prev) =>
      prev.map((r) => (r.row.id === rowId ? { ...r, state: { ...r.state, ...patch } } : r)),
    );
  }, []);

  const onValueChange = useCallback(
    (rowId: number, override: Record<string, unknown> | null) => {
      setRowState(rowId, { override });
    },
    [setRowState],
  );

  const onApproveRow = useCallback(
    (rowId: number) => {
      setRowState(rowId, { decision: 'approved' });
    },
    [setRowState],
  );

  const onRejectRow = useCallback(
    (rowId: number, reason: string) => {
      setRowState(rowId, { decision: 'rejected', rejectReason: reason });
    },
    [setRowState],
  );

  const allCitations = useMemo(() => _collectCitations(rows), [rows]);
  const synthesizedParagraphs = useMemo(() => _synthesizeParagraphs(rows), [rows]);

  // Citation chip → set activeIndex.
  const onCitationClick = useCallback(
    (citationFieldId: string, _page: string | null) => {
      const idx = allCitations.findIndex(
        (c) => c.field_or_chunk_id === citationFieldId,
      );
      if (idx >= 0) setActiveCitationIndex(idx);
      else if (allCitations.length > 0) setActiveCitationIndex(0);
      void _page;
    },
    [allCitations],
  );

  const liveRows = useMemo(
    () => rows.filter((r) => r.state.status === 'idle' || r.state.status === 'busy'),
    [rows],
  );

  const onSaveAll = useCallback(async () => {
    if (rows.length === 0) return;
    setBulkBusy(true);
    setBulkSummary(null);
    let nApproved = 0;
    let nRejected = 0;
    let nFailed = 0;
    const liveSnapshot = rows.filter((r) => r.state.status !== 'done');
    for (const lr of liveSnapshot) {
      const id = lr.row.id;
      setRowState(id, { status: 'busy' });
      try {
        if (lr.state.decision === 'rejected') {
          const reason = lr.state.rejectReason ?? 'Operator review — rejected';
          await rejectOne(baseUrl, id, reason);
          setRowState(id, { status: 'done' });
          nRejected += 1;
        } else {
          await approveOne(baseUrl, id, lr.state.override ?? undefined);
          setRowState(id, { status: 'done' });
          nApproved += 1;
        }
      } catch (err) {
        setRowState(id, {
          status: 'error',
          error: err instanceof Error ? err.message : 'Failed.',
        });
        nFailed += 1;
      }
    }
    setBulkSummary(
      `Approved ${nApproved} · Rejected ${nRejected}${nFailed ? ` · Failed ${nFailed}` : ''}`,
    );
    setBulkBusy(false);

    let rag: PostApprovalContext | null = null;
    if (nFailed === 0 && nApproved > 0) {
      try {
        rag = await fetchPostApprovalContext(baseUrl, documentReferenceId, {
          patient_id: patientId,
          document_reference_id: documentReferenceId,
        });
      } catch (err) {
        console.warn('[DocumentReviewPanel] post-approval-context failed', err);
      }
    }
    onCompleted(documentReferenceId, rag);
  }, [baseUrl, documentReferenceId, onCompleted, patientId, rows, setRowState]);

  const allDone = rows.length > 0 && rows.every((r) => r.state.status === 'done');
  const pendingCount = liveRows.length;

  const bboxLayout: BboxLayoutBlock[] = []; // not currently surfaced from staging payload
  const activeCitation = allCitations[activeCitationIndex];

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Review document extractions"
      style={overlayStyle}
      onClick={onClose}
    >
      <div style={panelStyle} onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div style={headerStyle}>
          <div style={titleBlockStyle}>
            <h2 style={titleStyle}>Review document extractions</h2>
            <code style={docRefStyle}>{documentReferenceId}</code>
          </div>
          <span style={pendingPillStyle(pendingCount)}>
            {pendingCount === 0
              ? 'All decided'
              : `${pendingCount} fact${pendingCount === 1 ? '' : 's'} pending`}
          </span>
          <button type="button" aria-label="Close" onClick={onClose} style={closeBtnStyle}>
            ×
          </button>
        </div>
        <div style={subHeaderStyle}>
          Batch <code style={{ fontSize: 12 }}>{fileBatchId}</code> · {rows.length} row
          {rows.length === 1 ? '' : 's'}
          {liveRows.length !== rows.length ? ` (${liveRows.length} live)` : ''}
        </div>

        {/* Body — two columns (responsive). */}
        <div style={bodyStyle}>
          {/* Left rail: source viewer */}
          <div style={leftRailStyle}>
            {/* Citation chip strip */}
            {allCitations.length > 0 && (
              <div style={chipStripStyle}>
                {allCitations.map((c, idx) => {
                  const active = idx === activeCitationIndex;
                  return (
                    <button
                      key={`${c.field_or_chunk_id}-${idx}`}
                      type="button"
                      onClick={() => setActiveCitationIndex(idx)}
                      style={chipStyle(active)}
                      title={c.quote_or_value || c.field_or_chunk_id}
                    >
                      {c.page ? `p${c.page} · ` : ''}
                      {(c.quote_or_value || c.field_or_chunk_id || `#${idx + 1}`).slice(0, 40)}
                    </button>
                  );
                })}
              </div>
            )}

            <div style={leftRailBodyStyle}>
              {previewLoading && (
                <div style={previewMsgStyle}>Loading source preview…</div>
              )}
              {!previewLoading && previewError && (
                <div style={previewMsgStyle}>
                  Source preview unavailable; data still editable on the right.
                </div>
              )}
              {!previewLoading && !previewError && previewBytes && previewKind === 'pdf' && (
                <div style={pdfWrapStyle}>
                  <DocumentViewer
                    pdfBytes={previewBytes}
                    citations={allCitations}
                    activeIndex={activeCitationIndex}
                    onActiveIndexChange={setActiveCitationIndex}
                    bboxLayout={bboxLayout}
                  />
                </div>
              )}
              {!previewLoading && !previewError && previewKind === 'image' && imageBlobUrl && (
                <div style={imageWrapStyle}>
                  <img src={imageBlobUrl} alt="Source document" style={imageStyle} />
                  {activeCitation && (
                    <div style={citationCaptionStyle}>
                      Citation: {activeCitation.quote_or_value || activeCitation.field_or_chunk_id}
                    </div>
                  )}
                </div>
              )}
              {!previewLoading && !previewError && previewKind === 'docx' && (
                <DocxParagraphsViewer
                  paragraphs={synthesizedParagraphs}
                  highlightedIndex={
                    activeCitationIndex >= 0 && activeCitationIndex < synthesizedParagraphs.length
                      ? activeCitationIndex
                      : null
                  }
                />
              )}
              {!previewLoading && !previewError && previewKind === 'unknown' && previewBytes && (
                <div style={previewMsgStyle}>
                  Source format not previewable; citations remain available above.
                </div>
              )}
            </div>
          </div>

          {/* Right rail: editor cards */}
          <div style={rightRailStyle}>
            {loading && <div style={loadingStyle}>Loading staged rows…</div>}
            {loadError && (
              <div role="alert" style={errorBannerStyle}>
                {loadError}
              </div>
            )}
            {!loading && !loadError && rows.length === 0 && (
              <div style={loadingStyle}>No staged rows for this document.</div>
            )}
            {rows.map((lr) => {
              const Editor = resolveEditor(lr.row);
              if (!Editor) {
                return (
                  <div key={lr.row.id} style={fallbackCardStyle}>
                    <div style={{ fontSize: 13, fontWeight: 600 }}>
                      #{lr.row.id} {lr.row.target_resource_type}
                    </div>
                    <div style={{ fontSize: 12, color: SURFACE.muted, marginTop: 4 }}>
                      No editor available for this resource shape — approve as-is or skip.
                    </div>
                  </div>
                );
              }
              const editorProps: FieldEditorProps = {
                row: lr.row,
                busy: bulkBusy,
                onValueChange,
                onApprove: onApproveRow,
                onReject: onRejectRow,
                onCitationClick,
                status: lr.state.status,
                statusMessage:
                  lr.state.status === 'error'
                    ? lr.state.error
                    : lr.state.status === 'done'
                      ? lr.state.decision === 'rejected'
                        ? 'rejected'
                        : 'approved'
                      : lr.state.decision === 'rejected'
                        ? 'will reject'
                        : lr.state.decision === 'approved'
                          ? 'will approve'
                          : null,
              };
              return <Editor key={lr.row.id} {...editorProps} />;
            })}
          </div>
        </div>

        {/* Footer */}
        <div style={footerStyle}>
          {bulkSummary && (
            <span style={{ fontSize: 13, color: SURFACE.muted, flex: 1 }}>{bulkSummary}</span>
          )}
          {!bulkSummary && (
            <span style={{ fontSize: 13, color: SURFACE.muted, flex: 1 }}>
              {allDone
                ? 'All rows decided.'
                : `Ready to commit ${liveRows.length} row${liveRows.length === 1 ? '' : 's'}.`}
            </span>
          )}
          <button
            type="button"
            onClick={onClose}
            disabled={bulkBusy}
            style={cancelBtnStyle(bulkBusy)}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void onSaveAll()}
            disabled={bulkBusy || liveRows.length === 0}
            style={saveBtnStyle(bulkBusy || liveRows.length === 0)}
          >
            {bulkBusy ? 'Working…' : `Save & Approve (${liveRows.length})`}
          </button>
        </div>
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// Styles
// ────────────────────────────────────────────────────────────────────────────

const overlayStyle: React.CSSProperties = {
  position: 'fixed',
  inset: 0,
  zIndex: 1100,
  background: 'rgba(15, 23, 42, 0.45)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: 24,
};

const panelStyle: React.CSSProperties = {
  background: SURFACE.bg,
  border: `1px solid ${SURFACE.borderStrong}`,
  borderRadius: 12,
  width: 'min(1280px, 100%)',
  height: 'min(92vh, 920px)',
  display: 'flex',
  flexDirection: 'column',
  boxShadow: '0 16px 48px rgba(15, 23, 42, 0.25)',
  overflow: 'hidden',
};

const headerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  padding: '14px 20px',
  borderBottom: `1px solid ${SURFACE.border}`,
  background: SURFACE.bg,
};

const titleBlockStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  flex: 1,
  minWidth: 0,
};

const titleStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 16,
  fontWeight: 600,
  color: SURFACE.fgStrong,
  whiteSpace: 'nowrap',
};

const docRefStyle: React.CSSProperties = {
  fontSize: 12,
  color: SURFACE.muted,
  background: NEU.bg,
  border: `1px solid ${NEU.border}`,
  borderRadius: 4,
  padding: '3px 8px',
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  maxWidth: 320,
};

const pendingPillStyle = (count: number): React.CSSProperties => ({
  fontSize: 12,
  fontWeight: 600,
  padding: '4px 10px',
  borderRadius: 12,
  background: count === 0 ? '#dcfce7' : BRAND.tint,
  color: count === 0 ? '#166534' : BRAND.base,
  border: count === 0 ? '1px solid #bbf7d0' : `1px solid ${BRAND.base}`,
});

const closeBtnStyle: React.CSSProperties = {
  background: 'transparent',
  border: 'none',
  fontSize: 22,
  cursor: 'pointer',
  color: SURFACE.muted,
  lineHeight: 1,
  padding: 4,
};

const subHeaderStyle: React.CSSProperties = {
  padding: '8px 20px',
  fontSize: 12,
  color: SURFACE.muted,
  borderBottom: `1px solid ${SURFACE.border}`,
  background: SURFACE.panel,
};

const bodyStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'minmax(0, 1.2fr) minmax(0, 1fr)',
  flex: 1,
  minHeight: 0,
};

const leftRailStyle: React.CSSProperties = {
  borderRight: `1px solid ${SURFACE.border}`,
  background: SURFACE.bg,
  minHeight: 0,
  display: 'flex',
  flexDirection: 'column',
};

const chipStripStyle: React.CSSProperties = {
  display: 'flex',
  flexWrap: 'wrap',
  gap: 6,
  padding: '10px 14px',
  borderBottom: `1px solid ${SURFACE.border}`,
  background: SURFACE.panel,
  maxHeight: 92,
  overflowY: 'auto',
};

const chipStyle = (active: boolean): React.CSSProperties => ({
  fontSize: 11,
  padding: '4px 9px',
  borderRadius: 12,
  border: `1px solid ${active ? BRAND.base : SURFACE.borderStrong}`,
  background: active ? BRAND.base : '#fff',
  color: active ? BRAND.onBrand : SURFACE.fg,
  fontWeight: active ? 600 : 500,
  cursor: 'pointer',
  fontFamily: 'inherit',
  maxWidth: 240,
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
});

const leftRailBodyStyle: React.CSSProperties = {
  flex: 1,
  minHeight: 0,
  overflowY: 'auto',
  position: 'relative',
};

const pdfWrapStyle: React.CSSProperties = {
  position: 'relative',
  width: '100%',
  height: '100%',
};

const imageWrapStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  gap: 10,
  padding: 16,
};

const imageStyle: React.CSSProperties = {
  maxWidth: '100%',
  height: 'auto',
  border: `1px solid ${SURFACE.border}`,
  borderRadius: 6,
  background: '#fff',
};

const citationCaptionStyle: React.CSSProperties = {
  fontSize: 12,
  color: SURFACE.muted,
  textAlign: 'center',
  padding: '6px 12px',
  background: SURFACE.panel,
  border: `1px solid ${SURFACE.border}`,
  borderRadius: 6,
  maxWidth: '90%',
};

const previewMsgStyle: React.CSSProperties = {
  padding: 24,
  fontSize: 13,
  color: SURFACE.muted,
  fontStyle: 'italic',
  textAlign: 'center',
};

const rightRailStyle: React.CSSProperties = {
  overflowY: 'auto',
  padding: 16,
  display: 'flex',
  flexDirection: 'column',
  gap: 12,
  minHeight: 0,
  background: SURFACE.panel,
};

const loadingStyle: React.CSSProperties = {
  padding: 12,
  color: SURFACE.muted,
  fontSize: 13,
};

const errorBannerStyle: React.CSSProperties = {
  padding: 10,
  fontSize: 12,
  color: RED.text,
  background: RED.bg,
  border: `1px solid ${RED.border}`,
  borderRadius: 6,
};

const fallbackCardStyle: React.CSSProperties = {
  padding: '12px 14px',
  border: `1px dashed ${SURFACE.border}`,
  borderRadius: 8,
  background: SURFACE.bg,
};

const footerStyle: React.CSSProperties = {
  padding: '14px 20px',
  borderTop: `1px solid ${SURFACE.border}`,
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  background: SURFACE.bg,
  position: 'sticky',
  bottom: 0,
};

const cancelBtnStyle = (busy: boolean): React.CSSProperties => ({
  fontSize: 13,
  fontWeight: 500,
  padding: '8px 18px',
  borderRadius: 6,
  cursor: busy ? 'not-allowed' : 'pointer',
  background: '#fff',
  color: SURFACE.fgStrong,
  border: `1px solid ${SURFACE.borderStrong}`,
  fontFamily: 'inherit',
  opacity: busy ? 0.5 : 1,
  minHeight: 36,
});

const saveBtnStyle = (disabled: boolean): React.CSSProperties => ({
  fontSize: 13,
  padding: '8px 20px',
  borderRadius: 6,
  cursor: disabled ? 'not-allowed' : 'pointer',
  background: BRAND.base,
  color: BRAND.onBrand,
  border: `1px solid ${BRAND.base}`,
  fontFamily: 'inherit',
  fontWeight: 600,
  opacity: disabled ? 0.5 : 1,
  minHeight: 36,
  boxShadow: disabled ? 'none' : '0 1px 2px rgba(15, 23, 42, 0.08)',
});
