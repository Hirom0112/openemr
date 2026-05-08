/**
 * PendingExtractionsSidebar — persistent inbox of pending HITL extractions
 * for the current patient. Mirrors the data shape ApprovalModal renders, but
 * survives across upload sessions: the rows in copilot_pending_extractions
 * stay reachable after the post-upload modal is dismissed.
 *
 * Refresh strategy:
 *   - on patient change → refetch
 *   - on approve/reject (inline or via the existing ApprovalModal) → refetch
 *   - polling: 30 s while open, 60 s while closed (count-only stays fresh
 *     for the toggle badge). Pauses while document.hidden.
 *
 * Row review: clicking "Review" on a row hands a synthetic StagingMetadata
 * payload to the parent (containing every still-pending sibling row that
 * shares the same file_batch_id) so the existing ApprovalModal can render
 * the full batch — no duplicate UI.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from 'react';
import {
  approveOne,
  getPending,
  rejectOne,
  type PendingExtractionRow,
  type StagingMetadata,
} from '../api';
import { BRAND, NEU, RED, SURFACE, type Lane } from '../styles/tokens';

const POLL_MS_OPEN = 30_000;
const POLL_MS_CLOSED = 60_000;
const PANEL_WIDTH = 360;

interface Props {
  baseUrl: string;
  patientId: string | null;
  open: boolean;
  onClose: () => void;
  /**
   * Hands a staging payload up so the parent can launch the existing
   * ApprovalModal. The sidebar groups every still-pending sibling that
   * shares the row's file_batch_id, matching the modal's batch UX.
   */
  onReview: (staging: StagingMetadata, lane: Lane | null) => void;
  /** Reported on every successful list response so the parent can render
   *  a badge on its toggle button. */
  onCountChange: (count: number) => void;
}

type RowBusy = 'approve' | 'reject' | null;

export default function PendingExtractionsSidebar(props: Props): ReactElement | null {
  const { baseUrl, patientId, open, onClose, onReview, onCountChange } = props;

  const [rows, setRows] = useState<PendingExtractionRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyByRow, setBusyByRow] = useState<Record<number, RowBusy>>({});
  const inFlight = useRef(false);

  const refetch = useCallback(async (): Promise<void> => {
    if (!patientId || !baseUrl) {
      setRows([]);
      onCountChange(0);
      return;
    }
    if (inFlight.current) return;
    inFlight.current = true;
    setLoading(true);
    setError(null);
    try {
      const resp = await getPending(baseUrl, { patient_id: patientId, state: 'pending' });
      setRows(resp.rows);
      onCountChange(resp.rows.length);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load pending extractions.');
    } finally {
      setLoading(false);
      inFlight.current = false;
    }
  }, [baseUrl, patientId, onCountChange]);

  // Refetch on patient change.
  useEffect(() => {
    void refetch();
  }, [refetch]);

  // Polling. Stays mounted across open/close so the badge count is fresh.
  useEffect(() => {
    if (!patientId) return;
    const interval = open ? POLL_MS_OPEN : POLL_MS_CLOSED;
    const id = window.setInterval(() => {
      if (document.hidden) return;
      void refetch();
    }, interval);
    return () => window.clearInterval(id);
  }, [open, patientId, refetch]);

  const setBusy = useCallback((rowId: number, kind: RowBusy) => {
    setBusyByRow((prev) => ({ ...prev, [rowId]: kind }));
  }, []);

  const onApproveRow = useCallback(async (row: PendingExtractionRow) => {
    setBusy(row.id, 'approve');
    try {
      await approveOne(baseUrl, row.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Approve failed.');
    } finally {
      setBusy(row.id, null);
      await refetch();
    }
  }, [baseUrl, refetch, setBusy]);

  const onRejectRow = useCallback(async (row: PendingExtractionRow) => {
    const reason = window.prompt('Reject reason:', 'Operator review — rejected from inbox');
    if (!reason || !reason.trim()) return;
    setBusy(row.id, 'reject');
    try {
      await rejectOne(baseUrl, row.id, reason.trim());
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Reject failed.');
    } finally {
      setBusy(row.id, null);
      await refetch();
    }
  }, [baseUrl, refetch, setBusy]);

  const onReviewRow = useCallback((row: PendingExtractionRow) => {
    // Group every still-pending sibling for the same file_batch_id so the
    // modal renders the full batch rather than just one row.
    const siblings = rows
      .filter((r) => r.file_batch_id === row.file_batch_id && r.state === 'pending')
      .map((r) => r.id);
    const ids = siblings.length > 0 ? siblings : [row.id];
    const staging: StagingMetadata = {
      file_batch_id: row.file_batch_id,
      pending_extraction_ids: ids,
    };
    onReview(staging, null);
  }, [rows, onReview]);

  // Stable sort: by file_batch_id then by id, so siblings cluster.
  const sortedRows = useMemo(() => {
    return [...rows].sort((a, b) => {
      if (a.file_batch_id === b.file_batch_id) return a.id - b.id;
      return a.file_batch_id < b.file_batch_id ? -1 : 1;
    });
  }, [rows]);

  if (!open) return null;

  return (
    <aside
      role="complementary"
      aria-label="Pending extractions inbox"
      style={{
        position: 'fixed',
        top: 0,
        right: 0,
        bottom: 0,
        width: PANEL_WIDTH,
        maxWidth: 'calc(100% - 32px)',
        zIndex: 1080,
        background: SURFACE.bg,
        borderLeft: `1px solid ${SURFACE.borderStrong}`,
        boxShadow: '-12px 0 32px rgba(15, 23, 42, 0.12)',
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      <header
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '12px 14px',
          borderBottom: `1px solid ${SURFACE.border}`,
          background: SURFACE.panel,
        }}
      >
        <h2 style={{ margin: 0, fontSize: 14, fontWeight: 600, color: SURFACE.fgStrong, flex: 1 }}>
          Pending review
          {!loading && (
            <span style={{ marginLeft: 8, color: SURFACE.muted, fontWeight: 500, fontSize: 12 }}>
              ({rows.length})
            </span>
          )}
        </h2>
        <button
          type="button"
          onClick={() => void refetch()}
          disabled={loading}
          aria-label="Refresh inbox"
          title="Refresh"
          style={{
            background: 'transparent',
            border: `1px solid ${NEU.border}`,
            borderRadius: 4,
            padding: '4px 8px',
            fontSize: 11,
            color: SURFACE.muted,
            cursor: loading ? 'not-allowed' : 'pointer',
            fontFamily: 'inherit',
          }}
        >
          {loading ? '…' : '⟳'}
        </button>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close pending extractions inbox"
          style={{
            background: 'transparent',
            border: 'none',
            fontSize: 18,
            lineHeight: 1,
            color: SURFACE.muted,
            cursor: 'pointer',
            padding: 4,
          }}
        >
          ×
        </button>
      </header>

      <div style={{ overflowY: 'auto', flex: 1, padding: '6px 0' }}>
        {!patientId && (
          <div style={emptyStateStyle}>Select a patient to see pending extractions.</div>
        )}
        {patientId && error && (
          <div role="alert" style={errorStyle}>
            {error}
            <button type="button" onClick={() => void refetch()} style={retryButtonStyle}>
              Retry
            </button>
          </div>
        )}
        {patientId && !error && loading && rows.length === 0 && (
          <div style={emptyStateStyle}>Loading…</div>
        )}
        {patientId && !error && !loading && rows.length === 0 && (
          <div style={emptyStateStyle}>No pending extractions for this patient.</div>
        )}

        {sortedRows.map((row) => (
          <RowCard
            key={row.id}
            row={row}
            busy={busyByRow[row.id] ?? null}
            onApprove={() => void onApproveRow(row)}
            onReject={() => void onRejectRow(row)}
            onReview={() => onReviewRow(row)}
          />
        ))}
      </div>
    </aside>
  );
}

interface RowCardProps {
  row: PendingExtractionRow;
  busy: RowBusy;
  onApprove: () => void;
  onReject: () => void;
  onReview: () => void;
}

function RowCard({ row, busy, onApprove, onReject, onReview }: RowCardProps): ReactElement {
  const summary = summarisePayload(row.payload);
  const docName = derivedDocName(row);
  const disabled = busy !== null;
  return (
    <div
      style={{
        padding: '10px 14px',
        borderBottom: `1px solid ${SURFACE.border}`,
        display: 'flex',
        flexDirection: 'column',
        gap: 6,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: SURFACE.muted }}>
        <code style={{ fontSize: 10 }}>#{row.id}</code>
        <span style={{ fontWeight: 600, color: SURFACE.fgStrong }}>{row.target_resource_type}</span>
        <span>·</span>
        <span title={row.document_reference_id} style={ellipsisStyle}>{docName}</span>
      </div>
      <div
        title={summary}
        style={{
          fontSize: 12,
          color: SURFACE.fg,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {summary}
      </div>
      <div style={{ display: 'flex', gap: 6 }}>
        <button type="button" onClick={onReview} disabled={disabled} style={secondaryBtn(disabled)}>
          Review
        </button>
        <button type="button" onClick={onReject} disabled={disabled} style={rejectBtn(disabled)}>
          {busy === 'reject' ? '…' : 'Reject'}
        </button>
        <button type="button" onClick={onApprove} disabled={disabled} style={approveBtn(disabled)}>
          {busy === 'approve' ? '…' : 'Approve'}
        </button>
      </div>
    </div>
  );
}

// ── helpers ────────────────────────────────────────────────────────────────

function summarisePayload(payload: Record<string, unknown>): string {
  const candidates = ['display', 'name', 'code_text', 'value_quantity', 'value_string', 'description'];
  for (const k of candidates) {
    const v = payload[k];
    if (typeof v === 'string' && v.trim()) return v.trim().slice(0, 160);
    if (typeof v === 'number') return String(v);
  }
  // Observation FHIR shape: {code:{coding:[{display}]}, valueQuantity:{value, unit}}
  const code = payload['code'];
  if (code && typeof code === 'object') {
    const coding = (code as { coding?: unknown[] }).coding;
    if (Array.isArray(coding) && coding.length > 0) {
      const first = coding[0];
      if (first && typeof first === 'object') {
        const disp = (first as { display?: unknown }).display;
        if (typeof disp === 'string' && disp.trim()) {
          const vq = payload['valueQuantity'];
          if (vq && typeof vq === 'object') {
            const v = (vq as { value?: unknown }).value;
            const u = (vq as { unit?: unknown }).unit;
            if (v !== undefined) return `${disp.trim()}: ${v}${typeof u === 'string' ? ` ${u}` : ''}`;
          }
          return disp.trim().slice(0, 160);
        }
      }
    }
  }
  try {
    return JSON.stringify(payload).slice(0, 160);
  } catch {
    return '(payload preview unavailable)';
  }
}

function derivedDocName(row: PendingExtractionRow): string {
  // We don't have the OpenEMR document filename in the staging payload; show
  // the document_reference_id (e.g. "copilot:402") so the operator can
  // correlate with the documents tab. Truncate for the rail width.
  const ref = row.document_reference_id || '(no ref)';
  return ref.length > 22 ? `${ref.slice(0, 20)}…` : ref;
}

const emptyStateStyle: React.CSSProperties = {
  padding: '14px 16px',
  fontSize: 12,
  color: SURFACE.muted,
};

const errorStyle: React.CSSProperties = {
  margin: '8px 14px',
  padding: '8px 10px',
  fontSize: 12,
  color: RED.text,
  background: RED.bg,
  border: `1px solid ${RED.border}`,
  borderRadius: 6,
  display: 'flex',
  alignItems: 'center',
  gap: 8,
};

const retryButtonStyle: React.CSSProperties = {
  marginLeft: 'auto',
  fontSize: 11,
  padding: '3px 8px',
  background: '#fff',
  color: RED.text,
  border: `1px solid ${RED.border}`,
  borderRadius: 4,
  cursor: 'pointer',
  fontFamily: 'inherit',
};

const ellipsisStyle: React.CSSProperties = {
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  maxWidth: 180,
};

function secondaryBtn(disabled: boolean): React.CSSProperties {
  return {
    fontSize: 11,
    padding: '4px 10px',
    background: '#fff',
    color: SURFACE.fg,
    border: `1px solid ${NEU.border}`,
    borderRadius: 4,
    cursor: disabled ? 'not-allowed' : 'pointer',
    opacity: disabled ? 0.5 : 1,
    fontFamily: 'inherit',
  };
}

function rejectBtn(disabled: boolean): React.CSSProperties {
  return {
    fontSize: 11,
    padding: '4px 10px',
    background: '#fff',
    color: RED.text,
    border: `1px solid ${RED.border}`,
    borderRadius: 4,
    cursor: disabled ? 'not-allowed' : 'pointer',
    opacity: disabled ? 0.5 : 1,
    fontFamily: 'inherit',
  };
}

function approveBtn(disabled: boolean): React.CSSProperties {
  return {
    fontSize: 11,
    padding: '4px 12px',
    background: BRAND.base,
    color: BRAND.onBrand,
    border: `1px solid ${BRAND.base}`,
    borderRadius: 4,
    cursor: disabled ? 'not-allowed' : 'pointer',
    opacity: disabled ? 0.5 : 1,
    fontFamily: 'inherit',
    fontWeight: 600,
  };
}
