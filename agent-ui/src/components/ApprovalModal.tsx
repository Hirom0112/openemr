/**
 * Slice 9.8 — ApprovalModal.
 *
 * Triggered when /document/ingest returns 200 + `metadata.staging`. Loads the
 * staged rows by id, renders them with per-row Approve/Reject buttons plus a
 * bulk-action footer. Best-effort batch semantics: a single row failing does
 * NOT stop sibling rows. The modal stays open until the row list is drained
 * or the operator dismisses it.
 *
 * v1 deliberate cuts (todo.md Slice 9.8):
 *   - row virtualization deferred — slice list at 50 rows
 *   - per-record diff viewer deferred — JSON payload preview only
 *   - undo deferred — terminal states are sticky
 */

import { useCallback, useEffect, useMemo, useState, type ReactElement } from 'react';
import {
  approveBatch,
  approveOne,
  getPendingOne,
  rejectOne,
  type BatchApproveResultItem,
  type PendingExtractionRow,
  type PendingState,
  type StagingMetadata,
} from '../api';
import LaneChip from './LaneChip';
import { type Lane, BRAND, NEU, RED, SURFACE } from '../styles/tokens';
import { record } from '../lib/telemetry';

const ROW_LIMIT = 50;

export interface ApprovalModalProps {
  baseUrl: string;
  staging: StagingMetadata | null;
  /** Lane label shown in the header (DOCUMENT/HL7/WORKBOOK). */
  lane?: Lane | null;
  onClose: () => void;
}

type RowStatus =
  | { kind: 'idle' }
  | { kind: 'busy' }
  | { kind: 'done'; state: PendingState; writeError?: string | null }
  | { kind: 'error'; message: string };

interface LoadedRow {
  row: PendingExtractionRow;
  status: RowStatus;
  reason: string; // reject reason draft, per-row
}

function _terminal(state: PendingState): boolean {
  return state === 'rejected' || state === 'written';
}

export default function ApprovalModal(props: ApprovalModalProps): ReactElement | null {
  const { baseUrl, staging, lane, onClose } = props;
  const [rows, setRows] = useState<LoadedRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkSummary, setBulkSummary] = useState<string | null>(null);

  // Load rows whenever the staging set changes. Bound to ROW_LIMIT to keep
  // first paint quick — operators with >50 staged rows are deferred until
  // the v1.5 inbox lands.
  useEffect(() => {
    if (!staging) {
      setRows([]);
      return;
    }
    let cancelled = false;
    const ids = staging.pending_extraction_ids.slice(0, ROW_LIMIT);
    setLoading(true);
    setLoadError(null);
    record({ name: 'approval_open', count: ids.length, lane: lane ?? undefined });
    void (async () => {
      try {
        const fetched = await Promise.all(ids.map((id) => getPendingOne(baseUrl, id).catch((err) => ({ _err: err, _id: id }))));
        if (cancelled) return;
        const loaded: LoadedRow[] = fetched.map((r) => {
          if (r && typeof r === 'object' && '_err' in r) {
            const placeholder = {
              id: (r as { _id: number })._id,
              document_reference_id: '',
              file_batch_id: staging.file_batch_id,
              patient_id: '',
              target_resource_type: 'Observation' as const,
              target_resource_id: '',
              state: 'pending' as PendingState,
              payload: {},
            };
            return {
              row: placeholder,
              status: {
                kind: 'error',
                message: (r as { _err: unknown })._err instanceof Error ? ((r as { _err: Error })._err).message : 'load failed',
              },
              reason: '',
            };
          }
          const row = r as PendingExtractionRow;
          return {
            row,
            status: _terminal(row.state)
              ? { kind: 'done', state: row.state, writeError: row.write_error }
              : { kind: 'idle' },
            reason: '',
          };
        });
        setRows(loaded);
      } catch (err) {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : 'Failed to load staged rows.');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [staging, baseUrl, lane]);

  const liveRows = useMemo(() => rows.filter((r) => r.status.kind === 'idle' || r.status.kind === 'busy'), [rows]);

  const setStatus = useCallback((id: number, status: RowStatus) => {
    setRows((prev) => prev.map((r) => (r.row.id === id ? { ...r, status } : r)));
  }, []);

  const setReason = useCallback((id: number, reason: string) => {
    setRows((prev) => prev.map((r) => (r.row.id === id ? { ...r, reason } : r)));
  }, []);

  const onApproveOne = useCallback(async (id: number) => {
    setStatus(id, { kind: 'busy' });
    record({ name: 'approval_action', outcome: 'success', count: 1, lane: lane ?? undefined });
    try {
      const out = await approveOne(baseUrl, id);
      setStatus(id, { kind: 'done', state: out.state, writeError: out.write_error });
    } catch (err) {
      setStatus(id, { kind: 'error', message: err instanceof Error ? err.message : 'Approve failed.' });
      record({ name: 'approval_action', outcome: 'error', lane: lane ?? undefined });
    }
  }, [baseUrl, lane, setStatus]);

  const onRejectOne = useCallback(async (id: number, reason: string) => {
    const trimmed = reason.trim();
    if (!trimmed) {
      setStatus(id, { kind: 'error', message: 'Reject reason is required.' });
      return;
    }
    setStatus(id, { kind: 'busy' });
    try {
      const out = await rejectOne(baseUrl, id, trimmed);
      setStatus(id, { kind: 'done', state: out.state });
    } catch (err) {
      setStatus(id, { kind: 'error', message: err instanceof Error ? err.message : 'Reject failed.' });
      record({ name: 'approval_action', outcome: 'error', lane: lane ?? undefined });
    }
  }, [baseUrl, lane, setStatus]);

  const onBulkApprove = useCallback(async () => {
    const ids = liveRows.map((r) => r.row.id);
    if (ids.length === 0) return;
    setBulkBusy(true);
    setBulkSummary(null);
    // Optimistically mark all as busy.
    ids.forEach((id) => setStatus(id, { kind: 'busy' }));
    try {
      const out = await approveBatch(baseUrl, ids);
      // Map results back to row state.
      const byId = new Map<number, BatchApproveResultItem>(out.results.map((it) => [it.pending_id, it]));
      ids.forEach((id) => {
        const it = byId.get(id);
        if (!it) {
          setStatus(id, { kind: 'error', message: 'no result returned' });
          return;
        }
        if (it.error) {
          setStatus(id, { kind: 'error', message: it.error });
          return;
        }
        setStatus(id, { kind: 'done', state: it.state, writeError: it.write_error });
      });
      setBulkSummary(`Approved ${out.n_approved} · Failed ${out.n_failed}`);
      record({ name: 'approval_action', outcome: out.n_failed === 0 ? 'success' : 'partial', count: ids.length, lane: lane ?? undefined });
    } catch (err) {
      // Whole batch failed (network) — restore rows to idle so the operator can retry.
      ids.forEach((id) => setStatus(id, {
        kind: 'error',
        message: err instanceof Error ? err.message : 'Batch approve failed.',
      }));
      record({ name: 'approval_action', outcome: 'error', count: ids.length, lane: lane ?? undefined });
    } finally {
      setBulkBusy(false);
    }
  }, [baseUrl, liveRows, lane, setStatus]);

  const onBulkReject = useCallback(async () => {
    const reason = window.prompt('Reject reason for all live rows:', 'Operator review — rejected as a batch');
    if (!reason || !reason.trim()) return;
    const trimmed = reason.trim();
    const targets = liveRows.map((r) => r.row.id);
    if (targets.length === 0) return;
    setBulkBusy(true);
    setBulkSummary(null);
    let nOk = 0;
    let nErr = 0;
    for (const id of targets) {
      setStatus(id, { kind: 'busy' });
      try {
        const out = await rejectOne(baseUrl, id, trimmed);
        setStatus(id, { kind: 'done', state: out.state });
        nOk += 1;
      } catch (err) {
        setStatus(id, { kind: 'error', message: err instanceof Error ? err.message : 'Reject failed.' });
        nErr += 1;
      }
    }
    setBulkBusy(false);
    setBulkSummary(`Rejected ${nOk} · Failed ${nErr}`);
    record({ name: 'approval_action', outcome: nErr === 0 ? 'success' : 'partial', count: targets.length, lane: lane ?? undefined });
  }, [baseUrl, liveRows, lane, setStatus]);

  if (!staging) return null;

  const handleDismiss = (): void => {
    record({ name: 'approval_close', count: liveRows.length, lane: lane ?? undefined });
    onClose();
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Approve staged extractions"
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 1100,
        background: 'rgba(15, 23, 42, 0.45)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 24,
      }}
      onClick={handleDismiss}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: SURFACE.bg,
          border: `1px solid ${SURFACE.borderStrong}`,
          borderRadius: 10,
          width: 'min(900px, 100%)',
          maxHeight: '85vh',
          display: 'flex',
          flexDirection: 'column',
          boxShadow: '0 16px 48px rgba(15, 23, 42, 0.25)',
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            padding: '14px 18px',
            borderBottom: `1px solid ${SURFACE.border}`,
          }}
        >
          <h2 style={{ margin: 0, fontSize: 15, fontWeight: 600, color: SURFACE.fgStrong, flex: 1 }}>
            Review staged extractions
          </h2>
          <LaneChip lane={lane ?? null} size="md" />
          <button
            type="button"
            aria-label="Close approval modal"
            onClick={handleDismiss}
            style={{
              background: 'transparent',
              border: 'none',
              fontSize: 18,
              cursor: 'pointer',
              color: SURFACE.muted,
              lineHeight: 1,
              padding: 4,
            }}
          >
            ×
          </button>
        </div>

        <div style={{ padding: '10px 18px', fontSize: 12, color: SURFACE.muted, borderBottom: `1px solid ${SURFACE.border}` }}>
          Batch <code style={{ fontSize: 11 }}>{staging.file_batch_id}</code> · {rows.length} row
          {rows.length === 1 ? '' : 's'}{liveRows.length !== rows.length ? ` (${liveRows.length} live)` : ''}
        </div>

        <div style={{ overflowY: 'auto', padding: '8px 18px', flex: 1 }}>
          {loading && <div style={{ color: SURFACE.muted, fontSize: 12, padding: '12px 0' }}>Loading staged rows…</div>}
          {loadError && (
            <div role="alert" style={{ color: RED.text, fontSize: 12, padding: '8px 10px', background: RED.bg, border: `1px solid ${RED.border}`, borderRadius: 6 }}>
              {loadError}
            </div>
          )}
          {!loading && !loadError && rows.length === 0 && (
            <div style={{ color: SURFACE.muted, fontSize: 12, padding: '12px 0' }}>No staged rows for this batch.</div>
          )}

          {rows.map((r) => (
            <Row
              key={r.row.id}
              row={r.row}
              status={r.status}
              reason={r.reason}
              onReasonChange={(v) => setReason(r.row.id, v)}
              onApprove={() => void onApproveOne(r.row.id)}
              onReject={() => void onRejectOne(r.row.id, r.reason)}
              disabled={bulkBusy}
            />
          ))}
        </div>

        <div
          style={{
            padding: '12px 18px',
            borderTop: `1px solid ${SURFACE.border}`,
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            background: SURFACE.panel,
          }}
        >
          {bulkSummary && (
            <span style={{ fontSize: 12, color: SURFACE.muted, flex: 1 }}>{bulkSummary}</span>
          )}
          {!bulkSummary && (
            <span style={{ fontSize: 12, color: SURFACE.muted, flex: 1 }}>
              Bulk actions act on the {liveRows.length} live row{liveRows.length === 1 ? '' : 's'}.
            </span>
          )}
          <button
            type="button"
            onClick={() => void onBulkReject()}
            disabled={bulkBusy || liveRows.length === 0}
            style={{
              fontSize: 12,
              padding: '6px 12px',
              minHeight: 30,
              borderRadius: 6,
              cursor: bulkBusy || liveRows.length === 0 ? 'not-allowed' : 'pointer',
              background: '#fff',
              color: RED.text,
              border: `1px solid ${RED.border}`,
              opacity: bulkBusy || liveRows.length === 0 ? 0.5 : 1,
              fontFamily: 'inherit',
            }}
          >
            Reject all
          </button>
          <button
            type="button"
            onClick={() => void onBulkApprove()}
            disabled={bulkBusy || liveRows.length === 0}
            style={{
              fontSize: 12,
              padding: '6px 14px',
              minHeight: 30,
              borderRadius: 6,
              cursor: bulkBusy || liveRows.length === 0 ? 'not-allowed' : 'pointer',
              background: BRAND.base,
              color: BRAND.onBrand,
              border: `1px solid ${BRAND.base}`,
              opacity: bulkBusy || liveRows.length === 0 ? 0.5 : 1,
              fontFamily: 'inherit',
              fontWeight: 600,
            }}
          >
            {bulkBusy ? 'Working…' : `Approve all (${liveRows.length})`}
          </button>
        </div>
      </div>
    </div>
  );
}

interface RowProps {
  row: PendingExtractionRow;
  status: RowStatus;
  reason: string;
  onReasonChange: (v: string) => void;
  onApprove: () => void;
  onReject: () => void;
  disabled: boolean;
}

function Row(props: RowProps): ReactElement {
  const { row, status, reason, onReasonChange, onApprove, onReject, disabled } = props;
  const summary = _summarise(row.payload);
  const stateLabel = status.kind === 'done' ? status.state : status.kind === 'busy' ? 'working…' : status.kind === 'error' ? 'error' : row.state;
  const isTerminal = status.kind === 'done' || status.kind === 'error';
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0,1fr) auto',
        gap: 10,
        alignItems: 'start',
        padding: '10px 0',
        borderBottom: `1px solid ${SURFACE.border}`,
      }}
    >
      <div style={{ minWidth: 0 }}>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 12, color: SURFACE.fgStrong }}>
          <code style={{ fontSize: 11, color: SURFACE.muted }}>#{row.id}</code>
          <span style={{ fontWeight: 600 }}>{row.target_resource_type}</span>
          <span style={{ color: SURFACE.muted }}>·</span>
          <span style={{ color: SURFACE.muted }}>{stateLabel}</span>
        </div>
        <div style={{ fontSize: 12, color: SURFACE.muted, marginTop: 2, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={summary}>
          {summary}
        </div>
        {status.kind === 'error' && (
          <div role="alert" style={{ fontSize: 11, color: RED.text, marginTop: 4 }}>
            {status.message}
          </div>
        )}
        {status.kind === 'done' && status.writeError && (
          <div role="alert" style={{ fontSize: 11, color: RED.text, marginTop: 4 }}>
            Write error: {status.writeError}
          </div>
        )}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
        {!isTerminal && (
          <>
            <input
              type="text"
              placeholder="Reject reason"
              value={reason}
              onChange={(e) => onReasonChange(e.target.value)}
              maxLength={256}
              disabled={disabled || status.kind === 'busy'}
              style={{
                fontSize: 11,
                padding: '4px 6px',
                width: 160,
                border: `1px solid ${NEU.border}`,
                borderRadius: 4,
                fontFamily: 'inherit',
              }}
            />
            <button
              type="button"
              onClick={onReject}
              disabled={disabled || status.kind === 'busy' || !reason.trim()}
              style={{
                fontSize: 11,
                padding: '4px 10px',
                background: '#fff',
                color: RED.text,
                border: `1px solid ${RED.border}`,
                borderRadius: 4,
                cursor: disabled || status.kind === 'busy' || !reason.trim() ? 'not-allowed' : 'pointer',
                fontFamily: 'inherit',
                opacity: disabled || status.kind === 'busy' || !reason.trim() ? 0.5 : 1,
              }}
            >
              Reject
            </button>
            <button
              type="button"
              onClick={onApprove}
              disabled={disabled || status.kind === 'busy'}
              style={{
                fontSize: 11,
                padding: '4px 10px',
                background: BRAND.base,
                color: BRAND.onBrand,
                border: `1px solid ${BRAND.base}`,
                borderRadius: 4,
                cursor: disabled || status.kind === 'busy' ? 'not-allowed' : 'pointer',
                fontFamily: 'inherit',
                fontWeight: 600,
                opacity: disabled || status.kind === 'busy' ? 0.5 : 1,
              }}
            >
              Approve
            </button>
          </>
        )}
        {isTerminal && status.kind === 'done' && (
          <span style={{ fontSize: 11, color: status.state === 'written' ? '#166534' : SURFACE.muted, fontWeight: 600 }}>
            {status.state}
          </span>
        )}
      </div>
    </div>
  );
}

function _summarise(payload: Record<string, unknown>): string {
  // Show one short line of payload context. Keys checked in priority order;
  // anything missing falls back to a JSON-ish stringified preview.
  const candidates = ['display', 'name', 'code_text', 'value_quantity', 'value_string', 'description'];
  for (const k of candidates) {
    const v = payload[k];
    if (typeof v === 'string' && v.trim()) return v.trim().slice(0, 120);
    if (typeof v === 'number') return String(v);
  }
  try {
    return JSON.stringify(payload).slice(0, 120);
  } catch {
    return '(payload preview unavailable)';
  }
}
