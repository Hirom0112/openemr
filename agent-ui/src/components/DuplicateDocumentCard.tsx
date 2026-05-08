/**
 * DuplicateDocumentCard.
 *
 * Triggered when /document/ingest returns 202 with `status: "processing"` and
 * no `quarantine_id` — i.e. the content-hash claim is held by another worker
 * (`agent-api/main.py:1685-1692` / `documents/store.py` `claim_path:
 * "in_flight_or_terminal"`). The document was already ingested (or is being
 * ingested) for this patient, so there is nothing for the operator to do
 * except confirm and navigate to the existing record.
 *
 * Distinct from QuarantineCard:
 *   - no Match / Reject affordances (there is nothing to match)
 *   - neutral / info color treatment instead of warning red
 *   - single primary affordance: open the Documents tab
 *
 * Navigation pattern mirrors QuarantineCard.tsx:
 *   - postMessage to the parent OpenEMR shell with type
 *     `copilot.openDocumentsTab`. The host decides actual routing — we do not
 *     hard-navigate from inside the iframe.
 */

import { useCallback, type ReactElement } from 'react';
import type { DuplicateIngestPayload } from '../api';
import LaneChip from './LaneChip';
import { BRAND, SURFACE, type Lane } from '../styles/tokens';

export interface DuplicateDocumentCardProps {
  payload: DuplicateIngestPayload | null;
  /** Lane for the original upload (DOCUMENT/HL7/WORKBOOK). */
  lane?: Lane | null;
  onClose: () => void;
}

export default function DuplicateDocumentCard(
  props: DuplicateDocumentCardProps,
): ReactElement | null {
  const { payload, lane, onClose } = props;

  const onOpenDocumentsTab = useCallback(() => {
    if (!payload) return;
    // Mirror QuarantineCard's deep-link pattern. The OpenEMR shell listens for
    // `copilot.openDocumentsTab` postMessages and handles the actual route.
    try {
      window.parent?.postMessage(
        {
          type: 'copilot.openDocumentsTab',
          document_reference_id: payload.document_reference_id,
          extraction_id: payload.extraction_id,
        },
        '*',
      );
    } catch {
      // best-effort; parent may not be listening in a standalone dev session
    }
  }, [payload]);

  if (!payload) return null;

  return (
    <div
      role="region"
      aria-label="Document already in patient record"
      style={{
        marginTop: 10,
        border: `1px solid ${BRAND.base}`,
        borderRadius: 8,
        background: BRAND.tint,
        padding: '12px 14px',
        display: 'flex',
        flexDirection: 'column',
        gap: 10,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span aria-hidden="true" style={{ fontSize: 14, color: BRAND.base }}>i</span>
        <strong style={{ fontSize: 13, color: BRAND.base, flex: 1 }}>
          Document already in patient record
        </strong>
        <LaneChip lane={lane ?? null} size="sm" />
        <button
          type="button"
          onClick={onClose}
          aria-label="Dismiss notification"
          style={{
            background: 'transparent',
            border: 'none',
            color: BRAND.base,
            cursor: 'pointer',
            fontSize: 16,
            lineHeight: 1,
            padding: 2,
          }}
        >
          ×
        </button>
      </div>

      <div style={{ fontSize: 12, color: SURFACE.fg, lineHeight: 1.5 }}>
        This document was previously ingested for this patient. View it in the
        Documents tab.
      </div>

      <div
        style={{
          fontSize: 11,
          color: SURFACE.muted,
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
        }}
      >
        document_reference_id: {payload.document_reference_id} · extraction_id:{' '}
        {payload.extraction_id}
      </div>

      <button
        type="button"
        onClick={onOpenDocumentsTab}
        style={{
          alignSelf: 'flex-start',
          fontSize: 12,
          padding: '6px 12px',
          background: BRAND.base,
          color: BRAND.onBrand,
          border: `1px solid ${BRAND.base}`,
          borderRadius: 4,
          cursor: 'pointer',
          fontFamily: 'inherit',
          fontWeight: 600,
        }}
      >
        Open Documents tab →
      </button>
    </div>
  );
}
