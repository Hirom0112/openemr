/**
 * DocumentsTab — Phase 2 unified review inbox.
 *
 * Lists every ingested document for the current patient that still has
 * pending HITL extractions, grouped by document_reference_id. Click "Review"
 * to open the existing ApprovalModal preloaded with that document's pending
 * row IDs (Phase 2 reuses the modal as the review surface; Phase 3 will swap
 * in a richer panel).
 *
 * Data source: `rows` is supplied by App-level polling of /pending-extractions
 * so the count badge on the tab strip and the cards here stay in sync.
 */

import { useMemo, type ReactElement } from 'react';
import type { PendingExtractionRow, StagingMetadata } from '../api';
import { BRAND, NEU, SURFACE, type Lane } from '../styles/tokens';

interface Props {
  rows: PendingExtractionRow[];
  patientId: string | null;
  /** Phase-2 fallback: HL7/XLSX/TIFF docs with Task or AllergyIntolerance
   *  rows route through the existing ApprovalModal. */
  onTriggerApproval: (staging: StagingMetadata, lane: Lane | null) => void;
  /** Phase-3 rich review surface: PDF/PNG/DOCX docs whose rows are
   *  Observation or IntakeFormField launch the inline review panel
   *  mounted at App-level. */
  onTriggerRichReview?: (
    documentReferenceId: string,
    fileBatchId: string,
    rowIds: number[],
    options?: { readOnly?: boolean; initialActiveCitationFieldId?: string },
  ) => void;
}

interface DocumentGroup {
  documentRef: string;
  fileBatchId: string;
  rowIds: number[];
  /** target_resource_type → count, used for the summary line. */
  byResource: Record<string, number>;
  /** True when every row in the group has a target_resource_type that the
   *  rich panel knows how to edit (Observation | IntakeFormField). */
  richEligible: boolean;
}

const RICH_ELIGIBLE_TYPES = new Set(['Observation', 'IntakeFormField']);

function groupRows(rows: PendingExtractionRow[]): DocumentGroup[] {
  const map = new Map<string, DocumentGroup>();
  for (const row of rows) {
    const key = row.document_reference_id || '(unknown)';
    let group = map.get(key);
    if (!group) {
      group = {
        documentRef: key,
        fileBatchId: row.file_batch_id,
        rowIds: [],
        byResource: {},
        richEligible: true,
      };
      map.set(key, group);
    }
    group.rowIds.push(row.id);
    const t = row.target_resource_type;
    group.byResource[t] = (group.byResource[t] ?? 0) + 1;
    if (!RICH_ELIGIBLE_TYPES.has(t)) {
      group.richEligible = false;
    }
  }
  // Stable order: by documentRef ascending.
  return [...map.values()].sort((a, b) =>
    a.documentRef < b.documentRef ? -1 : a.documentRef > b.documentRef ? 1 : 0,
  );
}

function summariseResources(byResource: Record<string, number>): string {
  const parts = Object.entries(byResource)
    .sort((a, b) => b[1] - a[1])
    .map(([resource, count]) => `${count} ${resource}${count === 1 ? '' : 's'} pending`);
  return parts.join(' • ');
}

export default function DocumentsTab(props: Props): ReactElement {
  const { rows, patientId, onTriggerApproval, onTriggerRichReview } = props;

  const groups = useMemo(() => groupRows(rows), [rows]);

  if (!patientId) {
    return (
      <div style={styles.empty}>
        Select a patient to see ingested documents.
      </div>
    );
  }

  if (groups.length === 0) {
    return (
      <div style={styles.empty}>
        No documents ingested for this patient yet.
      </div>
    );
  }

  return (
    <div style={styles.scroll}>
      <div style={styles.list}>
        {groups.map((group) => (
          <DocumentCard
            key={group.documentRef}
            group={group}
            onReview={() => {
              if (group.richEligible && onTriggerRichReview) {
                onTriggerRichReview(group.documentRef, group.fileBatchId, group.rowIds);
                return;
              }
              const staging: StagingMetadata = {
                file_batch_id: group.fileBatchId,
                pending_extraction_ids: group.rowIds,
              };
              onTriggerApproval(staging, null);
            }}
          />
        ))}
      </div>
    </div>
  );
}

interface DocumentCardProps {
  group: DocumentGroup;
  onReview: () => void;
}

function DocumentCard({ group, onReview }: DocumentCardProps): ReactElement {
  const total = group.rowIds.length;
  return (
    <div style={styles.card}>
      <div style={styles.cardHeader}>
        <code style={styles.docRef} title={group.documentRef}>
          {group.documentRef}
        </code>
        <span style={styles.statePill}>
          {total} fact{total === 1 ? '' : 's'} pending review
        </span>
      </div>
      <div style={styles.summary}>{summariseResources(group.byResource)}</div>
      <div style={styles.cardActions}>
        <button type="button" onClick={onReview} style={styles.reviewBtn}>
          Review
        </button>
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  scroll: {
    flex: 1,
    minHeight: 0,
    overflowY: 'auto',
    background: SURFACE.panel,
  },
  list: {
    display: 'flex',
    flexDirection: 'column',
    gap: 10,
    padding: 14,
  },
  empty: {
    padding: 24,
    color: SURFACE.muted,
    fontSize: 13,
  },
  card: {
    background: SURFACE.bg,
    border: `1px solid ${SURFACE.border}`,
    borderRadius: 8,
    padding: '12px 14px',
    display: 'flex',
    flexDirection: 'column',
    gap: 8,
  },
  cardHeader: {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    flexWrap: 'wrap',
  },
  docRef: {
    fontSize: 12,
    color: SURFACE.fgStrong,
    fontWeight: 600,
    background: NEU.bg,
    border: `1px solid ${NEU.border}`,
    borderRadius: 4,
    padding: '2px 6px',
    maxWidth: '100%',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  statePill: {
    fontSize: 11,
    fontWeight: 600,
    color: BRAND.base,
    background: BRAND.tint,
    border: `1px solid ${BRAND.base}`,
    borderRadius: 999,
    padding: '2px 10px',
  },
  summary: {
    fontSize: 12,
    color: SURFACE.muted,
  },
  cardActions: {
    display: 'flex',
    justifyContent: 'flex-end',
    gap: 8,
  },
  reviewBtn: {
    fontSize: 12,
    fontFamily: 'inherit',
    fontWeight: 600,
    padding: '6px 14px',
    background: BRAND.base,
    color: BRAND.onBrand,
    border: `1px solid ${BRAND.base}`,
    borderRadius: 4,
    cursor: 'pointer',
  },
};
