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
  /** Human label like "Whitaker Lab Report" / "Reyes Intake Form".
   *  Falls back to the documentRef when no demographics name is found. */
  displayLabel: string;
}

const RICH_ELIGIBLE_TYPES = new Set(['Observation', 'IntakeFormField']);

/** Pull demographics last name from a row whose target_resource_id matches
 *  the intake-demographics deterministic shape. Returns "" when no name
 *  is present. */
function _lastNameFromRow(row: PendingExtractionRow): string {
  if (!/-intake-demographics-\d+$/.test(row.target_resource_id ?? '')) return '';
  const p = row.payload as { demographics?: { name?: { value?: unknown } } };
  const raw = p.demographics?.name?.value;
  if (typeof raw !== 'string' || !raw.trim()) return '';
  // "WHITAKER, JAMES" → "Whitaker", "Margaret Chen" → "Chen".
  const comma = raw.indexOf(',');
  if (comma > 0) return raw.slice(0, comma).trim();
  const parts = raw.trim().split(/\s+/);
  return parts[parts.length - 1] ?? '';
}

function _titleCase(s: string): string {
  if (!s) return s;
  return s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();
}

/** Walk an arbitrary payload looking for the first dict that carries a
 *  `field_or_chunk_id` (or its FHIR-shape sibling `bbox_id`). Used by
 *  the format detector below — we don't care which field the citation
 *  hangs off, only what its locator string looks like. */
function _firstLocator(obj: unknown): string {
  if (!obj || typeof obj !== 'object') return '';
  if (Array.isArray(obj)) {
    for (const v of obj) {
      const f = _firstLocator(v);
      if (f) return f;
    }
    return '';
  }
  const r = obj as Record<string, unknown>;
  for (const k of ['field_or_chunk_id', 'bbox_id']) {
    const v = r[k];
    if (typeof v === 'string' && v) return v;
  }
  for (const v of Object.values(r)) {
    const f = _firstLocator(v);
    if (f) return f;
  }
  return '';
}

/** 'docx' (para=N locator), 'pdf' (pN-bNNN locator), or 'unknown'.
 *  Used to distinguish DOCX referral letters from PDF intake forms —
 *  both stage as IntakeFormField rows so target_resource_type alone
 *  can't tell them apart. */
function _formatHintFromRow(row: PendingExtractionRow): 'docx' | 'pdf' | 'unknown' {
  const id = _firstLocator(row.payload);
  if (!id) return 'unknown';
  if (/^para=\d+/.test(id)) return 'docx';
  if (/^p\d+-b\d+/.test(id)) return 'pdf';
  return 'unknown';
}

function _kindLabel(
  byResource: Record<string, number>,
  format: 'docx' | 'pdf' | 'unknown',
): string {
  if ((byResource.Observation ?? 0) > 0) return 'Lab';
  if ((byResource.IntakeFormField ?? 0) > 0) {
    return format === 'docx' ? 'Referral' : 'Intake';
  }
  if ((byResource.Task ?? 0) > 0) return 'Task';
  if ((byResource.AllergyIntolerance ?? 0) > 0) return 'Allergy';
  return 'Document';
}

function groupRows(rows: PendingExtractionRow[]): DocumentGroup[] {
  const map = new Map<string, DocumentGroup>();
  // Cached per-document last name; we walk demographics rows once per group.
  const lastNames = new Map<string, string>();
  // First citation-locator shape we see for the group decides the format.
  const formats = new Map<string, 'docx' | 'pdf' | 'unknown'>();
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
        displayLabel: key,
      };
      map.set(key, group);
    }
    group.rowIds.push(row.id);
    const t = row.target_resource_type;
    group.byResource[t] = (group.byResource[t] ?? 0) + 1;
    if (!RICH_ELIGIBLE_TYPES.has(t)) {
      group.richEligible = false;
    }
    if (!lastNames.has(key)) {
      const ln = _lastNameFromRow(row);
      if (ln) lastNames.set(key, ln);
    }
    if ((formats.get(key) ?? 'unknown') === 'unknown') {
      const f = _formatHintFromRow(row);
      if (f !== 'unknown') formats.set(key, f);
    }
  }
  // Compute display labels post-grouping so byResource is fully populated.
  for (const group of map.values()) {
    const last = lastNames.get(group.documentRef) ?? '';
    const fmt = formats.get(group.documentRef) ?? 'unknown';
    const kind = _kindLabel(group.byResource, fmt);
    group.displayLabel = last ? `${_titleCase(last)} ${kind}` : kind;
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
        <div style={styles.cardTitleBlock}>
          <span style={styles.docTitle} title={group.documentRef}>
            {group.displayLabel}
          </span>
          <code style={styles.docRefSub} title={group.documentRef}>
            {group.documentRef}
          </code>
        </div>
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
    justifyContent: 'space-between',
    gap: 10,
    flexWrap: 'wrap',
  },
  cardTitleBlock: {
    display: 'flex',
    flexDirection: 'column',
    gap: 2,
    minWidth: 0,
    flex: 1,
  },
  docTitle: {
    fontSize: 14,
    fontWeight: 600,
    color: SURFACE.fgStrong,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  docRefSub: {
    fontSize: 11,
    color: SURFACE.muted,
    fontFamily: 'monospace',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
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
