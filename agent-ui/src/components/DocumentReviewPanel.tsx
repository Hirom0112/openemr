/**
 * Phase-3 Documents tab — two-column rich review panel.
 *
 * Replaces ApprovalModal for PDF / PNG / DOCX rows whose target_resource_type
 * is one of {Observation, IntakeFormField}. HL7/XLSX/TIFF rows continue to
 * use ApprovalModal — those formats lack the per-field structure that
 * justifies the rich editor.
 *
 * Layout:
 *   ┌───────────────────────────────────┐
 *   │ Header: doc id + close            │
 *   ├───────────────┬───────────────────┤
 *   │ Source viewer │ Editor cards      │
 *   │ (left rail)   │ (right rail)      │
 *   ├───────────────┴───────────────────┤
 *   │ Footer: Cancel · Save & Approve  │
 *   └───────────────────────────────────┘
 *
 * v1 cuts:
 *   - PDF/PNG left rail is a placeholder; the staging row alone doesn't
 *     carry a reliable Binary URL. The full DocumentViewer integration
 *     can be wired when /document/{ref}/binary is exposed.
 *   - Citation deep-linking is best-effort: clicking "Source →" highlights
 *     a synthetic paragraph in the DOCX viewer when the row carries a
 *     citation reference; otherwise the click is a no-op.
 */

import { useCallback, useEffect, useMemo, useState, type ReactElement } from 'react';
import {
  approveOne,
  fetchPostApprovalContext,
  getPendingOne,
  rejectOne,
  type PendingExtractionRow,
  type PostApprovalContext,
} from '../api';
import DocxParagraphsViewer from './DocxParagraphsViewer';
import { resolveEditor, type FieldEditorProps } from './FieldEditors';
import { BRAND, NEU, RED, SURFACE } from '../styles/tokens';

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

const ROW_LIMIT = 50;

function _initialState(row: PendingExtractionRow): RowState {
  if (row.state === 'written' || row.state === 'approved') {
    return { decision: 'approved', override: null, rejectReason: null, status: 'done' };
  }
  if (row.state === 'rejected') {
    return { decision: 'rejected', override: null, rejectReason: null, status: 'done' };
  }
  return { decision: 'pending', override: null, rejectReason: null, status: 'idle' };
}

/**
 * Synthesize a paragraph list from a row payload's citations. v1 fallback
 * for the DOCX left rail when no full-document fetch is available.
 */
function _synthesizeParagraphs(rows: LoadedRow[]): string[] {
  const out: string[] = [];
  for (const r of rows) {
    const payload = r.row.payload as Record<string, unknown>;
    const value = (payload.value && typeof payload.value === 'object')
      ? (payload.value as Record<string, unknown>)
      : payload;
    const citations = Array.isArray(value.citations)
      ? value.citations
      : Array.isArray(payload.citations) ? payload.citations : [];
    for (const c of citations) {
      if (c && typeof c === 'object') {
        const q = (c as Record<string, unknown>).quote_or_value
          ?? (c as Record<string, unknown>).quote
          ?? (c as Record<string, unknown>).text;
        if (typeof q === 'string' && q.trim()) {
          out.push(q.trim());
        }
      }
    }
  }
  return out;
}

function _isDocxLike(rows: LoadedRow[]): boolean {
  // IntakeFormField rows come from documents whose source_format the staging
  // call defaults to "pdf" but DOCX flows through the same pipeline. We treat
  // any IntakeFormField group as DOCX-viewable; PDF/PNG with bbox layout
  // would route to DocumentViewer (deferred to v1.5).
  return rows.some((r) => r.row.target_resource_type === 'IntakeFormField');
}

export default function DocumentReviewPanel(props: DocumentReviewPanelProps): ReactElement {
  const { baseUrl, patientId, documentReferenceId, fileBatchId, pendingRowIds, onClose, onCompleted } = props;

  const [rows, setRows] = useState<LoadedRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkSummary, setBulkSummary] = useState<string | null>(null);
  const [highlightedParagraph, setHighlightedParagraph] = useState<number | null>(null);

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
    return () => { cancelled = true; };
  }, [baseUrl, pendingRowIds]);

  const setRowState = useCallback((rowId: number, patch: Partial<RowState>) => {
    setRows((prev) => prev.map((r) => (r.row.id === rowId ? { ...r, state: { ...r.state, ...patch } } : r)));
  }, []);

  const onValueChange = useCallback((rowId: number, override: Record<string, unknown> | null) => {
    setRowState(rowId, { override });
  }, [setRowState]);

  const onApproveRow = useCallback((rowId: number) => {
    setRowState(rowId, { decision: 'approved' });
  }, [setRowState]);

  const onRejectRow = useCallback((rowId: number, reason: string) => {
    setRowState(rowId, { decision: 'rejected', rejectReason: reason });
  }, [setRowState]);

  const onCitationClick = useCallback((citationFieldId: string, _page: string | null) => {
    // For v1 (DOCX path), we surface the synthetic paragraph index that
    // matches the row owning this citation. Look up the row by citation id
    // → its paragraph slot in the synthesized list. Best-effort only.
    const paragraphs = _synthesizeParagraphs(rows);
    // Trivial heuristic: highlight the first paragraph for now. The v1.5
    // viewer wire-up will resolve citationFieldId properly.
    const idx = paragraphs.length > 0 ? 0 : null;
    setHighlightedParagraph(idx);
    // Touch the param so an unused-variable lint doesn't fire.
    void citationFieldId;
  }, [rows]);

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
    // Iterate sequentially — keeps the success/fail rows visually coherent
    // and avoids slamming the writer with N parallel approves on large
    // intake forms. The list is bounded at ROW_LIMIT.
    const liveSnapshot = rows.filter((r) => r.state.status !== 'done');
    for (const lr of liveSnapshot) {
      const id = lr.row.id;
      // Update state via current row map so we don't fight stale closures.
      setRowState(id, { status: 'busy' });
      try {
        if (lr.state.decision === 'rejected') {
          const reason = lr.state.rejectReason ?? 'Operator review — rejected';
          await rejectOne(baseUrl, id, reason);
          setRowState(id, { status: 'done' });
          nRejected += 1;
        } else {
          // Default to approve for any row not explicitly rejected. Carry
          // the override_payload only when the editor produced one.
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
    setBulkSummary(`Approved ${nApproved} · Rejected ${nRejected}${nFailed ? ` · Failed ${nFailed}` : ''}`);
    setBulkBusy(false);

    // Fire the post-approval RAG once the batch lands. Errors are non-fatal
    // — the caller still receives a null rag result and the panel closes.
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

  const isDocx = useMemo(() => _isDocxLike(rows), [rows]);
  const synthesizedParagraphs = useMemo(() => _synthesizeParagraphs(rows), [rows]);
  const allDone = rows.length > 0 && rows.every((r) => r.state.status === 'done');

  return (
    <div role="dialog" aria-modal="true" aria-label="Review document extractions" style={overlayStyle} onClick={onClose}>
      <div style={panelStyle} onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div style={headerStyle}>
          <h2 style={titleStyle}>Review document extractions</h2>
          <code style={docRefStyle}>{documentReferenceId}</code>
          <button type="button" aria-label="Close" onClick={onClose} style={closeBtnStyle}>×</button>
        </div>
        <div style={subHeaderStyle}>
          Batch <code style={{ fontSize: 11 }}>{fileBatchId}</code> · {rows.length} row{rows.length === 1 ? '' : 's'}
          {liveRows.length !== rows.length ? ` (${liveRows.length} live)` : ''}
        </div>

        {/* Body — two columns */}
        <div style={bodyStyle}>
          {/* Left rail: source viewer */}
          <div style={leftRailStyle}>
            {isDocx ? (
              <DocxParagraphsViewer
                paragraphs={synthesizedParagraphs}
                highlightedIndex={highlightedParagraph}
              />
            ) : (
              <div style={pdfPlaceholderStyle}>
                Source preview unavailable in v1; use citation chips for context.
              </div>
            )}
          </div>

          {/* Right rail: editor cards */}
          <div style={rightRailStyle}>
            {loading && (
              <div style={loadingStyle}>Loading staged rows…</div>
            )}
            {loadError && (
              <div role="alert" style={errorBannerStyle}>{loadError}</div>
            )}
            {!loading && !loadError && rows.length === 0 && (
              <div style={loadingStyle}>No staged rows for this document.</div>
            )}
            {rows.map((lr) => {
              const Editor = resolveEditor(lr.row);
              if (!Editor) {
                return (
                  <div key={lr.row.id} style={fallbackCardStyle}>
                    <div style={{ fontSize: 12, fontWeight: 600 }}>
                      #{lr.row.id} {lr.row.target_resource_type}
                    </div>
                    <div style={{ fontSize: 11, color: SURFACE.muted, marginTop: 4 }}>
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
                  lr.state.status === 'error' ? lr.state.error
                  : lr.state.status === 'done'
                    ? (lr.state.decision === 'rejected' ? 'rejected' : 'approved')
                  : lr.state.decision === 'rejected' ? 'will reject'
                  : lr.state.decision === 'approved' ? 'will approve'
                  : null,
              };
              return <Editor key={lr.row.id} {...editorProps} />;
            })}
          </div>
        </div>

        {/* Footer */}
        <div style={footerStyle}>
          {bulkSummary && <span style={{ fontSize: 12, color: SURFACE.muted, flex: 1 }}>{bulkSummary}</span>}
          {!bulkSummary && (
            <span style={{ fontSize: 12, color: SURFACE.muted, flex: 1 }}>
              {allDone ? 'All rows decided.' : `Ready to commit ${liveRows.length} row${liveRows.length === 1 ? '' : 's'}.`}
            </span>
          )}
          <button type="button" onClick={onClose} disabled={bulkBusy} style={cancelBtnStyle(bulkBusy)}>
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
  borderRadius: 10,
  width: 'min(1200px, 100%)',
  height: 'min(90vh, 900px)',
  display: 'flex',
  flexDirection: 'column',
  boxShadow: '0 16px 48px rgba(15, 23, 42, 0.25)',
  overflow: 'hidden',
};

const headerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  padding: '14px 18px',
  borderBottom: `1px solid ${SURFACE.border}`,
};

const titleStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 15,
  fontWeight: 600,
  color: SURFACE.fgStrong,
};

const docRefStyle: React.CSSProperties = {
  fontSize: 11,
  color: SURFACE.muted,
  background: NEU.bg,
  border: `1px solid ${NEU.border}`,
  borderRadius: 4,
  padding: '2px 6px',
  fontFamily: 'inherit',
  flex: 1,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

const closeBtnStyle: React.CSSProperties = {
  background: 'transparent',
  border: 'none',
  fontSize: 18,
  cursor: 'pointer',
  color: SURFACE.muted,
  lineHeight: 1,
  padding: 4,
};

const subHeaderStyle: React.CSSProperties = {
  padding: '10px 18px',
  fontSize: 12,
  color: SURFACE.muted,
  borderBottom: `1px solid ${SURFACE.border}`,
};

const bodyStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: '1fr 1fr',
  flex: 1,
  minHeight: 0,
};

const leftRailStyle: React.CSSProperties = {
  borderRight: `1px solid ${SURFACE.border}`,
  overflowY: 'auto',
  background: SURFACE.panel,
  minHeight: 0,
};

const rightRailStyle: React.CSSProperties = {
  overflowY: 'auto',
  padding: 14,
  display: 'flex',
  flexDirection: 'column',
  gap: 10,
  minHeight: 0,
};

const pdfPlaceholderStyle: React.CSSProperties = {
  padding: 24,
  fontSize: 12,
  color: SURFACE.muted,
  fontStyle: 'italic',
};

const loadingStyle: React.CSSProperties = {
  padding: 12,
  color: SURFACE.muted,
  fontSize: 12,
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
  padding: '10px 12px',
  border: `1px dashed ${SURFACE.border}`,
  borderRadius: 6,
  background: SURFACE.panel,
};

const footerStyle: React.CSSProperties = {
  padding: '12px 18px',
  borderTop: `1px solid ${SURFACE.border}`,
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  background: SURFACE.panel,
};

const cancelBtnStyle = (busy: boolean): React.CSSProperties => ({
  fontSize: 12,
  padding: '6px 12px',
  borderRadius: 6,
  cursor: busy ? 'not-allowed' : 'pointer',
  background: '#fff',
  color: SURFACE.fgStrong,
  border: `1px solid ${SURFACE.borderStrong}`,
  fontFamily: 'inherit',
  opacity: busy ? 0.5 : 1,
});

const saveBtnStyle = (disabled: boolean): React.CSSProperties => ({
  fontSize: 12,
  padding: '6px 14px',
  borderRadius: 6,
  cursor: disabled ? 'not-allowed' : 'pointer',
  background: BRAND.base,
  color: BRAND.onBrand,
  border: `1px solid ${BRAND.base}`,
  fontFamily: 'inherit',
  fontWeight: 600,
  opacity: disabled ? 0.5 : 1,
});
