/**
 * Slice 9.8 — QuarantineCard.
 *
 * Triggered when /document/ingest returns 202. Distinct surface from
 * PostIngestContextCard — the document is in quarantine, not in the chart.
 *
 * Renders:
 *   - reason-code copy table (mrn_not_found / ambiguous / parse_failed / ...)
 *   - parsed-identity hint summary (so the operator sees what the resolver
 *     thought it saw)
 *   - mini inline form: enter target patient_id → POST quarantine match
 *   - reject button → POST quarantine reject
 *   - "Open in Documents tab" — postMessage to parent window so the OpenEMR
 *     shell can navigate. We do NOT do a hard navigate from inside the
 *     iframe; the host decides routing.
 */

import { useCallback, useState, type ReactElement } from 'react';
import {
  quarantineMatch,
  quarantineReject,
  type QuarantineIngestPayload,
} from '../api';
import LaneChip from './LaneChip';
import { BRAND, NEU, RED, SURFACE, type Lane } from '../styles/tokens';
import { record } from '../lib/telemetry';

const REASON_COPY: Record<string, { title: string; body: string }> = {
  mrn_not_found: {
    title: 'No patient matched the MRN on this document',
    body: 'The MRN parsed from the file did not resolve to any patient on your panel. Match it to the correct patient or reject if the document does not belong here.',
  },
  ambiguous_match: {
    title: 'Multiple candidate patients matched',
    body: 'Name + DOB resolution returned more than one candidate. Pick the right patient explicitly to break the tie.',
  },
  parse_failed: {
    title: 'Demographics could not be parsed',
    body: 'No MRN / name / DOB combination could be extracted from this file. Match it manually if the source is trusted.',
  },
  panel_violation: {
    title: 'Resolved patient is outside your panel',
    body: 'The MRN matched a patient who is not on your panel. Match only to patients on your panel; otherwise reject.',
  },
  default: {
    title: 'Document held for manual review',
    body: 'Server flagged this upload for operator triage. Use Match to route it, or Reject to discard.',
  },
};

export interface QuarantineCardProps {
  baseUrl: string;
  payload: QuarantineIngestPayload | null;
  /** Lane for the original upload (DOCUMENT/HL7/WORKBOOK). */
  lane?: Lane | null;
  /** Filename hash from the upload, for telemetry only. Never the raw name. */
  filenameHash?: string;
  onClose: () => void;
  /** Fired after a successful match — caller may want to refetch context. */
  onMatched?: (resolvedPatientId: string) => void;
}

type ActionState =
  | { kind: 'idle' }
  | { kind: 'busy' }
  | { kind: 'matched'; resolvedPatientId: string }
  | { kind: 'rejected' }
  | { kind: 'error'; message: string };

export default function QuarantineCard(props: QuarantineCardProps): ReactElement | null {
  const { baseUrl, payload, lane, filenameHash, onClose, onMatched } = props;
  const [targetPatientId, setTargetPatientId] = useState('');
  const [rejectReason, setRejectReason] = useState('');
  const [state, setState] = useState<ActionState>({ kind: 'idle' });

  const reasonCopy = payload ? (REASON_COPY[payload.reason_code] ?? REASON_COPY.default) : REASON_COPY.default;

  const onMatch = useCallback(async () => {
    if (!payload) return;
    const trimmed = targetPatientId.trim();
    if (!trimmed) {
      setState({ kind: 'error', message: 'Enter a target patient ID.' });
      return;
    }
    setState({ kind: 'busy' });
    record({ name: 'quarantine_action', outcome: 'success', reason_code: payload.reason_code, lane: lane ?? undefined, filename_hash: filenameHash });
    try {
      const out = await quarantineMatch(baseUrl, payload.quarantine_id, trimmed);
      setState({ kind: 'matched', resolvedPatientId: out.resolved_patient_id });
      if (onMatched) onMatched(out.resolved_patient_id);
    } catch (err) {
      setState({ kind: 'error', message: err instanceof Error ? err.message : 'Match failed.' });
      record({ name: 'quarantine_action', outcome: 'error', reason_code: payload.reason_code, lane: lane ?? undefined });
    }
  }, [baseUrl, payload, targetPatientId, lane, filenameHash, onMatched]);

  const onReject = useCallback(async () => {
    if (!payload) return;
    const trimmed = rejectReason.trim();
    if (!trimmed) {
      setState({ kind: 'error', message: 'Enter a reject reason.' });
      return;
    }
    setState({ kind: 'busy' });
    try {
      await quarantineReject(baseUrl, payload.quarantine_id, trimmed);
      setState({ kind: 'rejected' });
      record({ name: 'quarantine_action', outcome: 'success', reason_code: payload.reason_code, lane: lane ?? undefined });
    } catch (err) {
      setState({ kind: 'error', message: err instanceof Error ? err.message : 'Reject failed.' });
      record({ name: 'quarantine_action', outcome: 'error', reason_code: payload.reason_code, lane: lane ?? undefined });
    }
  }, [baseUrl, payload, rejectReason, lane]);

  const onOpenInDocumentsTab = useCallback(() => {
    if (!payload) return;
    // Deep-link affordance — postMessage to the parent OpenEMR shell.
    // The shell is responsible for actually navigating to the Documents tab
    // and selecting the quarantined item. We deliberately do NOT do a hard
    // window.location change from inside the iframe.
    try {
      window.parent?.postMessage(
        {
          type: 'copilot.openDocumentsTab',
          quarantine_id: payload.quarantine_id,
          document_reference_id: payload.document_reference_id,
        },
        '*',
      );
    } catch {
      // best-effort
    }
  }, [payload]);

  if (!payload) return null;

  const terminal = state.kind === 'matched' || state.kind === 'rejected';

  return (
    <div
      role="region"
      aria-label="Document held for manual review"
      style={{
        marginTop: 10,
        border: `1px solid ${RED.border}`,
        borderRadius: 8,
        background: RED.bg,
        padding: '12px 14px',
        display: 'flex',
        flexDirection: 'column',
        gap: 10,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span aria-hidden="true" style={{ fontSize: 14, color: RED.text }}>⚠</span>
        <strong style={{ fontSize: 13, color: RED.text, flex: 1 }}>{reasonCopy.title}</strong>
        <LaneChip lane={lane ?? null} size="sm" />
        <button
          type="button"
          onClick={onClose}
          aria-label="Dismiss quarantine card"
          style={{
            background: 'transparent',
            border: 'none',
            color: RED.text,
            cursor: 'pointer',
            fontSize: 16,
            lineHeight: 1,
            padding: 2,
          }}
        >
          ×
        </button>
      </div>
      <div style={{ fontSize: 12, color: RED.secondary, lineHeight: 1.5 }}>{reasonCopy.body}</div>

      <div style={{ fontSize: 11, color: SURFACE.muted, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>
        quarantine_id: {payload.quarantine_id} · reason_code: {payload.reason_code}
      </div>

      {Object.keys(payload.hint_summary || {}).length > 0 && (
        <details style={{ fontSize: 11, color: SURFACE.muted }}>
          <summary style={{ cursor: 'pointer' }}>Parsed identity hint</summary>
          <pre
            style={{
              fontSize: 10,
              background: SURFACE.bg,
              border: `1px solid ${SURFACE.border}`,
              borderRadius: 4,
              padding: '6px 8px',
              margin: '4px 0 0 0',
              maxHeight: 120,
              overflow: 'auto',
            }}
          >
            {JSON.stringify(payload.hint_summary, null, 2)}
          </pre>
        </details>
      )}

      {!terminal && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <input
              type="text"
              placeholder="Target patient ID"
              value={targetPatientId}
              onChange={(e) => setTargetPatientId(e.target.value)}
              disabled={state.kind === 'busy'}
              style={{
                flex: 1,
                fontSize: 12,
                padding: '5px 8px',
                border: `1px solid ${NEU.border}`,
                borderRadius: 4,
                fontFamily: 'inherit',
              }}
            />
            <button
              type="button"
              onClick={() => void onMatch()}
              disabled={state.kind === 'busy' || !targetPatientId.trim()}
              style={{
                fontSize: 12,
                padding: '5px 12px',
                background: BRAND.base,
                color: BRAND.onBrand,
                border: `1px solid ${BRAND.base}`,
                borderRadius: 4,
                cursor: state.kind === 'busy' || !targetPatientId.trim() ? 'not-allowed' : 'pointer',
                fontFamily: 'inherit',
                fontWeight: 600,
                opacity: state.kind === 'busy' || !targetPatientId.trim() ? 0.5 : 1,
              }}
            >
              Match
            </button>
          </div>

          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <input
              type="text"
              placeholder="Reject reason"
              value={rejectReason}
              onChange={(e) => setRejectReason(e.target.value)}
              maxLength={256}
              disabled={state.kind === 'busy'}
              style={{
                flex: 1,
                fontSize: 12,
                padding: '5px 8px',
                border: `1px solid ${NEU.border}`,
                borderRadius: 4,
                fontFamily: 'inherit',
              }}
            />
            <button
              type="button"
              onClick={() => void onReject()}
              disabled={state.kind === 'busy' || !rejectReason.trim()}
              style={{
                fontSize: 12,
                padding: '5px 12px',
                background: '#fff',
                color: RED.text,
                border: `1px solid ${RED.border}`,
                borderRadius: 4,
                cursor: state.kind === 'busy' || !rejectReason.trim() ? 'not-allowed' : 'pointer',
                fontFamily: 'inherit',
                opacity: state.kind === 'busy' || !rejectReason.trim() ? 0.5 : 1,
              }}
            >
              Reject
            </button>
          </div>

          <button
            type="button"
            onClick={onOpenInDocumentsTab}
            style={{
              alignSelf: 'flex-start',
              fontSize: 11,
              padding: '4px 10px',
              background: 'transparent',
              color: SURFACE.fg,
              border: `1px solid ${NEU.border}`,
              borderRadius: 4,
              cursor: 'pointer',
              fontFamily: 'inherit',
            }}
          >
            Open in Documents tab ↗
          </button>
        </div>
      )}

      {state.kind === 'matched' && (
        <div role="status" style={{ fontSize: 12, color: '#166534', fontWeight: 600 }}>
          Matched to patient <code>{state.resolvedPatientId}</code>.
        </div>
      )}
      {state.kind === 'rejected' && (
        <div role="status" style={{ fontSize: 12, color: SURFACE.muted, fontWeight: 600 }}>
          Document rejected.
        </div>
      )}
      {state.kind === 'error' && (
        <div role="alert" style={{ fontSize: 12, color: RED.text }}>
          {state.message}
        </div>
      )}
    </div>
  );
}
