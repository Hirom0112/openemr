/**
 * DocumentReviewPanel — clinician review surface for staged document
 * extractions. Replaces the old 816-line modal-card layout with a
 * full-bleed two-column experience matching docs/document-review-mock.html.
 *
 * Layout:
 *   ┌─ Top bar: breadcrumb · doc meta · status pill · open-in-tab ──────┐
 *   ├─ Document viewer (1fr, scrollable) ─┬─ Review rail (480px) ──────┤
 *   │   prev/next/zoom controls            │   header: "Extracted Fields"│
 *   │   PDF page / image / DOCX paragraphs │   field-list: grouped cards │
 *   │   citation overlays (bidirectional)  │   each card: label + chip + │
 *   │                                      │   inputs + confidence + 2 btns│
 *   ├──────────────────────────────────────┴─────────────────────────────┤
 *   │ Action bar: progress bar · "n of N reviewed" · Reject all · Approve all│
 *   └────────────────────────────────────────────────────────────────────┘
 *
 * Trigger contract is unchanged from the old panel — App.tsx mounts when
 * `reviewTarget && ingestPatientId` are set; props match the old interface
 * so ChatSurface (post-upload) and DocumentsTab (Review button) keep working.
 *
 * Design tokens (--bg, --accent, --warn, font families) live in a scoped
 * CSS-variable block on this component's root element so the rest of
 * agent-ui keeps its existing blue-brand palette unchanged.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactElement,
  type ReactNode,
} from 'react';
import {
  approveBatch,
  approveOne,
  createManualCitation,
  patchCitationBbox,
  fetchDocumentBinary,
  fetchDocxParagraphs,
  fetchPostApprovalContext,
  getPendingOne,
  rejectOne,
  type DocxParagraphPayload,
  type ManualCitationKind,
  type PendingExtractionRow,
  type PostApprovalContext,
} from '../api';
import { loadPdf, type PDFDocumentProxy } from '../lib/pdfjs';

export interface DocumentReviewPanelProps {
  baseUrl: string;
  patientId: string;
  documentReferenceId: string;
  fileBatchId: string;
  pendingRowIds: number[];
  /**
   * When true, the panel renders a verification-only view: inputs are
   * disabled, per-row Approve/Reject and the bulk action bar are
   * hidden, and the status pill flips to "Read-only · Approved". Used
   * by the chat citation chips so the clinician can revisit a cited
   * field after the document is approved without re-triggering edit
   * UI. Defaults to false.
   */
  readOnly?: boolean;
  /**
   * Optional citation `field_or_chunk_id` (e.g. "p1-b007") that the
   * panel should auto-focus on mount. The panel scans loaded rows to
   * find one whose primary citation matches and applies the active
   * highlight + scrolls the right rail to that card. Falls through
   * silently when no match is found.
   */
  initialActiveCitationFieldId?: string;
  onClose: () => void;
  onCompleted: (
    documentReferenceId: string,
    ragResult: PostApprovalContext | null,
  ) => void;
}

// ────────────────────────────────────────────────────────────────────────────
// Type internals
// ────────────────────────────────────────────────────────────────────────────

type ViewerKind = 'pdf' | 'image' | 'docx' | 'unknown';
type Decision = 'pending' | 'approved' | 'rejected';
type RowStatus = 'idle' | 'busy' | 'done' | 'error';

interface RowState {
  decision: Decision;
  override: Record<string, unknown> | null;
  rejectReason: string | null;
  status: RowStatus;
  error?: string;
}

interface LoadedRow {
  row: PendingExtractionRow;
  state: RowState;
}

interface FlatCitation {
  /** Unique render-side card id (see :type:`VirtualCard`). */
  cardId: string;
  /** Underlying staging row id — what approve/reject hits. */
  rowId: number;
  /** 1-based ordinal across the whole document — drives the chip label `#296`. */
  ordinal: number;
  page: number;
  bbox: [number, number, number, number] | null;
  fieldOrChunkId: string;
  quote: string;
  /** Short human label e.g. "demographics" / "lisinopril" — drives the bbox badge. */
  shortLabel: string;
  /** 1-based DOCX paragraph index, parsed from `page_or_section`'s
   *  `para=N` locator. Null for non-DOCX citations or when the locator is
   *  missing/unparseable; the docx preview falls back to quote-matching. */
  paraIdx: number | null;
}

/**
 * One render-tier card. The staging table has 1 row per intake KIND
 * (demographics gets a single row whose payload nests 5 fields), but
 * the panel renders 1 card per *field* — 5 demographics cards, 1
 * medication card per medication, and so on. Cards point back to a
 * single :type:`LoadedRow` for status + approve/reject routing; that
 * means clicking Approve on the "DOB" sub-card commits the whole
 * demographics row, and the other 4 demographics cards reflect the
 * shared status. We surface this in the UI by treating the row's
 * status as the card's status.
 */
interface VirtualCard {
  id: string;
  parent: LoadedRow;
  /** Demographics sub-field name; null for non-demographics cards. */
  subfield: 'name' | 'dob' | 'sex' | 'mrn' | 'address' | null;
  /** Per-card primary citation. May be null when the field has none. */
  citation: FlatCitation | null;
  category: string;
  /** Per-card label — "Name" / "Date of Birth" / "Lisinopril" / etc. */
  cardLabel: string;
}

const ROW_LIMIT = 50;
const PDF_MIME = 'application/pdf';
const PNG_MIME = 'image/png';
const JPEG_MIME = 'image/jpeg';
const DOCX_MIME =
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document';

const FONT_HREF =
  'https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600;9..144,700&family=Inter+Tight:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap';

// Mock category order. Anything we don't match falls into "Other".
const GROUP_ORDER: ReadonlyArray<string> = [
  'Demographics',
  'Chief Concern',
  'Current Medications',
  'Allergies',
  'Family History',
  'Code Status',
  'Lab Values',
  'Other',
];

function _initialState(row: PendingExtractionRow): RowState {
  if (row.state === 'written' || row.state === 'approved') {
    return { decision: 'approved', override: null, rejectReason: null, status: 'done' };
  }
  if (row.state === 'rejected') {
    return { decision: 'rejected', override: null, rejectReason: null, status: 'done' };
  }
  return { decision: 'pending', override: null, rejectReason: null, status: 'idle' };
}

function _detectKind(contentType: string | null): ViewerKind {
  if (!contentType) return 'unknown';
  const t = contentType.toLowerCase();
  if (t.includes(PDF_MIME)) return 'pdf';
  if (t.includes(PNG_MIME) || t.includes(JPEG_MIME) || t.includes('image/')) return 'image';
  if (t.includes(DOCX_MIME) || t.includes('officedocument.wordprocessingml')) return 'docx';
  return 'unknown';
}

function _payloadValue(row: PendingExtractionRow): Record<string, unknown> {
  const p = row.payload as Record<string, unknown>;
  // Demographics rows wrap the field bag under a `demographics` key.
  // The other intake kinds (medication/allergy/chief_concern/family_history/
  // code_status) are flat — fields and citations live at payload root.
  // Observation rows are FHIR-shaped and also use the root.
  const k = _kindFromRow(row);
  if (k === 'demographics') {
    const demo = p.demographics;
    if (demo && typeof demo === 'object' && !Array.isArray(demo)) {
      return demo as Record<string, unknown>;
    }
  }
  // Some legacy paths nested under `.value`. Prefer it when present and
  // it's an object (not the chief_concern scalar value).
  const v = p.value;
  if (v && typeof v === 'object' && !Array.isArray(v)) {
    return v as Record<string, unknown>;
  }
  return p;
}

function _kindFromRow(row: PendingExtractionRow): string {
  if (row.target_resource_type === 'Observation') return 'lab';
  if (row.target_resource_type !== 'IntakeFormField') return 'other';
  const payload = row.payload as { kind?: unknown };
  let k = typeof payload.kind === 'string' ? payload.kind : '';
  if (!k) {
    const m = /-intake-([\w-]+?)-\d+$/.exec(row.target_resource_id ?? '');
    if (m) k = m[1];
  }
  return k || 'other';
}

function _str(v: unknown, fallback = ''): string {
  if (typeof v === 'string') return v;
  if (v && typeof v === 'object' && 'value' in (v as Record<string, unknown>)) {
    const inner = (v as Record<string, unknown>).value;
    if (typeof inner === 'string') return inner;
  }
  return fallback;
}

const _kind = _kindFromRow;

function _categoryFor(row: PendingExtractionRow): string {
  switch (_kind(row)) {
    case 'demographics': return 'Demographics';
    case 'chief_concern': return 'Chief Concern';
    case 'medication': return 'Current Medications';
    case 'allergy': return 'Allergies';
    case 'family_history': return 'Family History';
    case 'code_status': return 'Code Status';
    case 'lab': return 'Lab Values';
    default: return 'Other';
  }
}

/** Short label that lands on the bbox badge. */
function _shortLabelFor(row: PendingExtractionRow): string {
  const v = _payloadValue(row);
  switch (_kind(row)) {
    case 'demographics': return _str(v.name) || 'demographics';
    case 'chief_concern': return 'reason';
    case 'medication': return _str(v.name) || 'medication';
    case 'allergy': return _str(v.substance) || 'allergy';
    case 'family_history': {
      const rel = _str(v.relation);
      const cond = _str(v.condition);
      return rel || cond || 'family-hx';
    }
    case 'code_status': return _str(v.status, 'code');
    case 'lab': {
      const code = row.payload as { code?: { coding?: Array<{ display?: string }> } };
      const display = code.code?.coding?.[0]?.display;
      return display || 'lab';
    }
    default: return row.target_resource_type;
  }
}

/** Walk an arbitrary object looking for the first non-empty `citations` array. */
function _findFirstCitation(obj: unknown): unknown | null {
  if (!obj || typeof obj !== 'object') return null;
  if (Array.isArray(obj)) {
    for (const item of obj) {
      const found = _findFirstCitation(item);
      if (found) return found;
    }
    return null;
  }
  const rec = obj as Record<string, unknown>;
  if (Array.isArray(rec.citations) && rec.citations.length > 0) {
    return rec.citations[0];
  }
  for (const key of Object.keys(rec)) {
    const found = _findFirstCitation(rec[key]);
    if (found) return found;
  }
  return null;
}

/** Normalize a single Citation dict from the staging payload into our
 *  internal shape. Returns null when the dict isn't shaped right. */
function _normalizeCitationDict(c: unknown): {
  page: number;
  bbox: [number, number, number, number] | null;
  fieldOrChunkId: string;
  quote: string;
  paraIdx: number | null;
} | null {
  if (!c || typeof c !== 'object') return null;
  const obj = c as Record<string, unknown>;
  const fieldOrChunkId =
    (typeof obj.field_or_chunk_id === 'string' && obj.field_or_chunk_id) ||
    (typeof obj.field_id === 'string' ? obj.field_id : '') ||
    '';
  const quote =
    (typeof obj.quote_or_value === 'string' && obj.quote_or_value) ||
    (typeof obj.quote === 'string' && obj.quote) ||
    (typeof obj.text === 'string' ? obj.text : '') || '';
  const pageNum = typeof obj.page === 'number' && Number.isFinite(obj.page) && obj.page >= 1
    ? Math.floor(obj.page)
    : (() => {
        const raw = typeof obj.page_or_section === 'string' ? obj.page_or_section : null;
        if (raw == null) return 1;
        const m = /\d+/.exec(raw);
        return m ? Math.max(1, parseInt(m[0], 10)) : 1;
      })();
  const bbox = Array.isArray(obj.bbox) && obj.bbox.length === 4
    ? (obj.bbox as [number, number, number, number])
    : null;
  // DOCX citations carry a synthetic `para=N` (or `para=N|run=M`) locator. Per
  // the prose-extractor contract (intake_form_prose.py:41-45) it's emitted in
  // `field_or_chunk_id`; `page_or_section` carries the section name. Older
  // ingests may have emitted it in either field, so we accept both. Extract
  // para N when present so the docx preview branch can map citation → paragraph.
  let paraIdx: number | null = null;
  const _matchPara = (s: string | null): number | null => {
    if (!s) return null;
    const m = /\bpara=(\d+)/.exec(s);
    return m ? parseInt(m[1], 10) : null;
  };
  paraIdx =
    _matchPara(fieldOrChunkId) ??
    _matchPara(typeof obj.page_or_section === 'string' ? obj.page_or_section : null);
  return { page: pageNum, bbox, fieldOrChunkId, quote, paraIdx };
}

/** Pull the first/primary citation off a row payload. */
function _primaryCitation(row: PendingExtractionRow): {
  page: number;
  bbox: [number, number, number, number] | null;
  fieldOrChunkId: string;
  quote: string;
  paraIdx: number | null;
} | null {
  const v = _payloadValue(row);
  // Try root.citations / value.citations first; fall back to a depth-walk
  // for nested shapes (e.g. demographics whose fields each carry their own
  // citations under payload.demographics.<field>.citations).
  const direct = Array.isArray(v.citations)
    ? v.citations
    : Array.isArray((row.payload as Record<string, unknown>).citations)
      ? ((row.payload as Record<string, unknown>).citations as unknown[])
      : null;
  const c0 = direct ? direct[0] : _findFirstCitation(row.payload);
  return _normalizeCitationDict(c0);
}

const _DEMOGRAPHICS_SUBFIELDS: ReadonlyArray<{
  key: 'name' | 'dob' | 'sex' | 'mrn' | 'address';
  label: string;
}> = [
  { key: 'name',    label: 'Name' },
  { key: 'dob',     label: 'Date of Birth' },
  { key: 'sex',     label: 'Sex' },
  { key: 'mrn',     label: 'MRN' },
  { key: 'address', label: 'Address' },
];

/**
 * For a demographics row, expand its nested ``payload.demographics``
 * blob into per-subfield card definitions. Each sub-field has its
 * own citation (different bbox/page) — pulling them apart at the
 * card layer means the rail shows one card + one chip + one bbox per
 * field rather than smashing all 5 into a single card.
 *
 * Subfields that are absent in the payload are omitted entirely (no
 * empty Sex card when the LLM didn't extract one).
 */
function _demographicsSubCards(
  row: PendingExtractionRow,
): Array<{
  subfield: 'name' | 'dob' | 'sex' | 'mrn' | 'address';
  label: string;
  value: string;
  citation: ReturnType<typeof _normalizeCitationDict>;
}> {
  const demo = _payloadValue(row);
  const out: Array<{
    subfield: 'name' | 'dob' | 'sex' | 'mrn' | 'address';
    label: string;
    value: string;
    citation: ReturnType<typeof _normalizeCitationDict>;
  }> = [];
  for (const { key, label } of _DEMOGRAPHICS_SUBFIELDS) {
    const sub = demo[key];
    if (sub === undefined || sub === null) continue;
    let value = '';
    let citation: ReturnType<typeof _normalizeCitationDict> = null;
    if (typeof sub === 'string') {
      value = sub;
    } else if (typeof sub === 'object' && !Array.isArray(sub)) {
      const subRec = sub as Record<string, unknown>;
      value = typeof subRec.value === 'string' ? subRec.value : '';
      const cits = Array.isArray(subRec.citations) ? subRec.citations : [];
      citation = _normalizeCitationDict(cits[0]);
    }
    if (!value) continue;
    out.push({ subfield: key, label, value, citation });
  }
  return out;
}

function _shortLabelForSubfield(
  subfield: 'name' | 'dob' | 'sex' | 'mrn' | 'address',
  value: string,
): string {
  switch (subfield) {
    case 'name': return value || 'name';
    case 'dob': return 'dob';
    case 'sex': return value || 'sex';
    case 'mrn': return 'mrn';
    case 'address': return 'address';
  }
}

/** Synthesize paragraphs for the DOCX fallback. */
function _synthesizeParagraphs(rows: LoadedRow[]): string[] {
  const out: string[] = [];
  for (const r of rows) {
    const c = _primaryCitation(r.row);
    if (c?.quote) out.push(c.quote);
  }
  return out;
}

/** Inject a stylesheet link once, dedupe by id. Returns a cleanup fn. */
function _injectFonts(): () => void {
  const id = 'copilot-review-fonts';
  let el = document.getElementById(id) as HTMLLinkElement | null;
  let owned = false;
  if (!el) {
    el = document.createElement('link');
    el.id = id;
    el.rel = 'stylesheet';
    el.href = FONT_HREF;
    document.head.appendChild(el);
    owned = true;
  }
  return () => {
    if (owned && el && el.parentNode) {
      el.parentNode.removeChild(el);
    }
  };
}

// ────────────────────────────────────────────────────────────────────────────
// Main component
// ────────────────────────────────────────────────────────────────────────────

export default function DocumentReviewPanel(
  props: DocumentReviewPanelProps,
): ReactElement {
  const {
    baseUrl,
    patientId,
    documentReferenceId,
    fileBatchId,
    pendingRowIds,
    readOnly = false,
    initialActiveCitationFieldId,
    onClose,
    onCompleted,
  } = props;

  const [rows, setRows] = useState<LoadedRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Source preview state
  const [previewBytes, setPreviewBytes] = useState<ArrayBuffer | null>(null);
  const [previewKind, setPreviewKind] = useState<ViewerKind>('unknown');
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [imageBlobUrl, setImageBlobUrl] = useState<string | null>(null);
  const [docxParagraphs, setDocxParagraphs] = useState<DocxParagraphPayload[] | null>(null);

  // Active row drives the bbox highlight + scroll-into-view in the rail.
  const [activeCardId, setActiveCardId] = useState<string | null>(null);

  // Feature B — bbox reshape mode. ``editMode`` toggles 8 resize handles
  // on every citation; ``editedBboxes`` holds the current optimistic
  // override per cardId so the layer stays interactive while we PATCH.
  // The optimistic state is keyed by cardId because a single staging
  // row can have N citations (demographics splits 5 ways) — keying on
  // rowId would conflate them. A 'failed' patch reverts the override.
  const [editMode, setEditMode] = useState(false);
  const [editedBboxes, setEditedBboxes] = useState<
    Record<string, [number, number, number, number]>
  >({});

  // Feature A — manual citation create. ``creatingMode`` switches the
  // doc-stage into draw-a-rectangle mode; on mouse-up the rectangle is
  // captured into ``creatingDraft`` and the create-form modal opens.
  // Submit fires POST /pending-extractions/manual and the new row is
  // prepended to ``rows`` so the rail card appears immediately.
  const [creatingMode, setCreatingMode] = useState(false);
  const [creatingDraft, setCreatingDraft] = useState<{
    bbox: [number, number, number, number];
    page: number;
  } | null>(null);

  // PDF page navigation. The user can drive this with prev/next OR by
  // clicking a field whose citation lives on a different page.
  const [activePage, setActivePage] = useState<number>(1);
  const [zoom, setZoom] = useState<number>(1.0);

  // Bulk-action state
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkSummary, setBulkSummary] = useState<string | null>(null);

  // Inject Google Fonts once for the lifetime of the panel.
  useEffect(() => _injectFonts(), []);

  // Esc to close, `e` toggles bbox edit mode.
  useEffect(() => {
    const handler = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') {
        if (!bulkBusy) onClose();
        return;
      }
      if (e.key !== 'e' && e.key !== 'E') return;
      // Don't toggle while typing into an input/textarea.
      const tgt = e.target as HTMLElement | null;
      if (tgt) {
        const tag = tgt.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        if (tgt.isContentEditable) return;
      }
      if (readOnly) return;
      setEditMode((m) => !m);
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onClose, bulkBusy, readOnly]);

  // Load pending rows.
  useEffect(() => {
    let cancelled = false;
    const ids = pendingRowIds.slice(0, ROW_LIMIT);
    if (ids.length === 0) {
      setRows([]);
      return;
    }
    setLoading(true);
    setLoadError(null);
    void (async () => {
      try {
        const fetched = await Promise.all(
          ids.map((id) =>
            getPendingOne(baseUrl, id).catch((err) => ({ _err: err, _id: id })),
          ),
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
        if (!cancelled) {
          setLoadError(err instanceof Error ? err.message : 'Failed to load rows.');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [baseUrl, pendingRowIds]);

  // Fetch source preview bytes.
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

  // Fetch parsed DOCX paragraphs once the preview kind is known to be docx.
  // Server-side parsing keeps the locator grammar (para=N) consistent with
  // what the extractor cited; falling back to synthesizedParagraphs when the
  // fetch fails preserves the v1 behavior.
  useEffect(() => {
    let cancelled = false;
    setDocxParagraphs(null);
    if (!documentReferenceId || previewKind !== 'docx') return;
    void (async () => {
      try {
        const { paragraphs } = await fetchDocxParagraphs(baseUrl, documentReferenceId);
        if (cancelled) return;
        setDocxParagraphs(paragraphs);
      } catch {
        if (cancelled) return;
        setDocxParagraphs(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [baseUrl, documentReferenceId, previewKind]);

  // Image blob URL plumbing.
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

  // ── Card expansion ─────────────────────────────────────────────────────
  // Demographics rows split into one card per nested sub-field so each
  // citation gets its own chip + bbox; everything else is 1 card per row.
  const cards: VirtualCard[] = useMemo(() => {
    const out: VirtualCard[] = [];
    let ordinal = 0;
    for (const lr of rows) {
      const k = _kind(lr.row);
      if (k === 'demographics') {
        const subs = _demographicsSubCards(lr.row);
        for (const s of subs) {
          ordinal += 1;
          const cardId = `${lr.row.id}:${s.subfield}`;
          out.push({
            id: cardId,
            parent: lr,
            subfield: s.subfield,
            citation: s.citation
              ? {
                  cardId,
                  rowId: lr.row.id,
                  ordinal,
                  page: s.citation.page,
                  bbox: s.citation.bbox,
                  fieldOrChunkId: s.citation.fieldOrChunkId,
                  quote: s.citation.quote,
                  shortLabel: _shortLabelForSubfield(s.subfield, s.value),
                  paraIdx: s.citation.paraIdx,
                }
              : null,
            category: 'Demographics',
            cardLabel: s.label,
          });
        }
      } else {
        ordinal += 1;
        const cardId = `${lr.row.id}`;
        const c = _primaryCitation(lr.row);
        out.push({
          id: cardId,
          parent: lr,
          subfield: null,
          citation: c
            ? {
                cardId,
                rowId: lr.row.id,
                ordinal,
                page: c.page,
                bbox: c.bbox,
                fieldOrChunkId: c.fieldOrChunkId,
                quote: c.quote,
                shortLabel: _shortLabelFor(lr.row),
                paraIdx: c.paraIdx,
              }
            : null,
          category: _categoryFor(lr.row),
          cardLabel: _labelFor(lr.row),
        });
      }
    }
    return out;
  }, [rows]);

  // Flat citation list — drives the bbox layer + initial-citation focus.
  const cardCitations = useMemo(
    () => cards.map((c) => c.citation).filter((c): c is FlatCitation => c !== null),
    [cards],
  );

  // Citations on the active page (PDF only).
  const activePageCitations = useMemo(
    () => cardCitations.filter((c) => c.page === activePage),
    [cardCitations, activePage],
  );

  // When the panel mounts in read-only mode and the caller passed a
  // citation field id (e.g. "p1-b007"), find the card whose citation
  // matches and auto-focus it. Runs ONCE per (cards-built,
  // initialActiveCitationFieldId) — re-running on activeCardId change
  // would fight the user's clicks.
  useEffect(() => {
    if (!initialActiveCitationFieldId || cardCitations.length === 0) return;
    const match = cardCitations.find(
      (c) => c.fieldOrChunkId === initialActiveCitationFieldId,
    );
    if (!match) return;
    setActiveCardId(match.cardId);
    if (match.page !== activePage) setActivePage(match.page);
    // Best-effort scroll the rail card into view after paint.
    if (typeof window !== 'undefined') {
      window.requestAnimationFrame(() => {
        const el = document.getElementById(`copilot-review-field-${match.cardId}`);
        el?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialActiveCitationFieldId, cardCitations.length]);

  // ── Row mutation callbacks ─────────────────────────────────────────────
  const setRowState = useCallback((rowId: number, patch: Partial<RowState>) => {
    setRows((prev) =>
      prev.map((r) =>
        r.row.id === rowId ? { ...r, state: { ...r.state, ...patch } } : r,
      ),
    );
  }, []);

  const onValueChange = useCallback(
    (rowId: number, override: Record<string, unknown> | null) => {
      setRowState(rowId, { override });
    },
    [setRowState],
  );

  // Approve / reject — single-row, fires immediately against the staging API.
  const onApproveOne = useCallback(
    async (rowId: number) => {
      const lr = rows.find((r) => r.row.id === rowId);
      if (!lr || lr.state.status === 'busy' || lr.state.status === 'done') return;
      setRowState(rowId, { status: 'busy', decision: 'approved' });
      try {
        await approveOne(baseUrl, rowId, lr.state.override ?? undefined);
        setRowState(rowId, { status: 'done' });
      } catch (err) {
        setRowState(rowId, {
          status: 'error',
          error: err instanceof Error ? err.message : 'Approve failed.',
        });
      }
    },
    [baseUrl, rows, setRowState],
  );

  const onRejectOne = useCallback(
    async (rowId: number) => {
      const reason = window.prompt('Reject reason:', 'Operator review — rejected');
      if (!reason || !reason.trim()) return;
      const trimmed = reason.trim();
      setRowState(rowId, { status: 'busy', decision: 'rejected', rejectReason: trimmed });
      try {
        await rejectOne(baseUrl, rowId, trimmed);
        setRowState(rowId, { status: 'done' });
      } catch (err) {
        setRowState(rowId, {
          status: 'error',
          error: err instanceof Error ? err.message : 'Reject failed.',
        });
      }
    },
    [baseUrl, setRowState],
  );

  // ── Bidirectional citation linking ─────────────────────────────────────
  const onFieldClick = useCallback(
    (cardId: string) => {
      setActiveCardId(cardId);
      const c = cardCitations.find((x) => x.cardId === cardId);
      if (c && c.page !== activePage) setActivePage(c.page);
    },
    [cardCitations, activePage],
  );

  const onBboxClick = useCallback((cardId: string) => {
    setActiveCardId(cardId);
    // Scroll the corresponding field into view.
    const el = document.getElementById(`copilot-review-field-${cardId}`);
    if (el && typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }, []);

  // ── Bbox reshape (Feature B) ───────────────────────────────────────────
  // Optimistic-update during drag: set the override on every mouse-move
  // tick. On mouse-up, the layer calls onBboxCommit which PATCHes the
  // backend and logs the same payload to the browser console. On
  // failure, the override is cleared so the rendered bbox snaps back to
  // the citation's source-of-truth value.
  const onBboxOptimistic = useCallback(
    (cardId: string, bbox: [number, number, number, number]) => {
      setEditedBboxes((prev) => ({ ...prev, [cardId]: bbox }));
    },
    [],
  );

  const onBboxCommit = useCallback(
    async (
      cardId: string,
      newBbox: [number, number, number, number],
      before: [number, number, number, number],
      page: number,
      deltaPx: { dx: number; dy: number; dw: number; dh: number },
    ) => {
      const card = cards.find((c) => c.id === cardId);
      if (!card || !card.citation) return;
      const cit = card.citation;
      const evt = {
        cardId,
        rowId: cit.rowId,
        page,
        before,
        after: newBbox,
        deltaPx,
        deltaPdfPoints: [
          Number((newBbox[0] - before[0]).toFixed(3)),
          Number((newBbox[1] - before[1]).toFixed(3)),
          Number((newBbox[2] - before[2]).toFixed(3)),
          Number((newBbox[3] - before[3]).toFixed(3)),
        ],
      };
      // eslint-disable-next-line no-console
      console.log('[bbox-edit]', evt);
      try {
        const resp = await patchCitationBbox(baseUrl, cit.rowId, {
          field_or_chunk_id: cit.fieldOrChunkId,
          page,
          bbox: newBbox,
        });
        // eslint-disable-next-line no-console
        console.log('[bbox-edit] PATCH ok', resp);
      } catch (err) {
        // eslint-disable-next-line no-console
        console.warn('[bbox-edit] PATCH failed — reverting', err);
        setEditedBboxes((prev) => {
          const next = { ...prev };
          delete next[cardId];
          return next;
        });
      }
    },
    [baseUrl, cards],
  );

  // ── Manual citation creation (Feature A) ──────────────────────────────
  const onCreateDraftFromDrag = useCallback(
    (bbox: [number, number, number, number], page: number) => {
      setCreatingDraft({ bbox, page });
      setCreatingMode(false);
    },
    [],
  );

  const onCreateCancel = useCallback(() => {
    setCreatingDraft(null);
    setCreatingMode(false);
  }, []);

  const onCreateSubmit = useCallback(
    async (form: {
      kind: ManualCitationKind;
      value: string;
      extra?: string;
    }) => {
      if (!creatingDraft) return;
      try {
        const resp = await createManualCitation(baseUrl, {
          patient_id: patientId,
          document_reference_id: documentReferenceId,
          file_batch_id: fileBatchId,
          kind: form.kind,
          value: form.value,
          extra: form.extra,
          page: creatingDraft.page,
          bbox: creatingDraft.bbox,
        });
        // Fetch the freshly-staged row so the rail picks it up. We
        // append rather than refetch all so existing card state
        // (active highlight, in-flight approves) isn't disturbed.
        const newRow = await getPendingOne(baseUrl, resp.pending_id);
        setRows((prev) => [
          ...prev,
          { row: newRow, state: _initialState(newRow) },
        ]);
        setCreatingDraft(null);
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error('[manual-create] failed', err);
        // Keep the draft open so the operator can edit + retry.
        throw err;
      }
    },
    [baseUrl, creatingDraft, documentReferenceId, fileBatchId, patientId],
  );

  // ── Bulk actions ───────────────────────────────────────────────────────
  const liveRows = useMemo(
    () => rows.filter((r) => r.state.status !== 'done'),
    [rows],
  );

  const reviewedCount = useMemo(
    () => rows.filter((r) => r.state.status === 'done').length,
    [rows],
  );

  const onApproveAll = useCallback(async () => {
    const ids = liveRows.map((r) => r.row.id);
    if (ids.length === 0) return;
    setBulkBusy(true);
    setBulkSummary(null);
    ids.forEach((id) => setRowState(id, { status: 'busy', decision: 'approved' }));
    try {
      const out = await approveBatch(baseUrl, ids);
      const byId = new Map(out.results.map((it) => [it.pending_id, it]));
      ids.forEach((id) => {
        const it = byId.get(id);
        if (!it) {
          setRowState(id, { status: 'error', error: 'no result' });
          return;
        }
        if (it.error) {
          setRowState(id, { status: 'error', error: it.error });
          return;
        }
        setRowState(id, { status: 'done' });
      });
      setBulkSummary(`Approved ${out.n_approved} · Failed ${out.n_failed}`);
      // Fire post-approval RAG once everything is committed.
      if (out.n_failed === 0 && out.n_approved > 0) {
        try {
          const rag = await fetchPostApprovalContext(baseUrl, documentReferenceId, {
            patient_id: patientId,
            document_reference_id: documentReferenceId,
          });
          onCompleted(documentReferenceId, rag);
        } catch (err) {
          console.warn('[DocumentReviewPanel] post-approval-context failed', err);
          onCompleted(documentReferenceId, null);
        }
      } else {
        onCompleted(documentReferenceId, null);
      }
    } catch (err) {
      ids.forEach((id) =>
        setRowState(id, {
          status: 'error',
          error: err instanceof Error ? err.message : 'Batch approve failed.',
        }),
      );
    } finally {
      setBulkBusy(false);
    }
  }, [baseUrl, documentReferenceId, liveRows, onCompleted, patientId, setRowState]);

  const onRejectAll = useCallback(async () => {
    const ids = liveRows.map((r) => r.row.id);
    if (ids.length === 0) return;
    const reason = window.prompt(
      `Reject all ${ids.length} extraction${ids.length === 1 ? '' : 's'}? Reason:`,
      'Operator review — rejected as a batch',
    );
    if (!reason || !reason.trim()) return;
    const trimmed = reason.trim();
    setBulkBusy(true);
    setBulkSummary(null);
    let nOk = 0;
    let nErr = 0;
    for (const id of ids) {
      setRowState(id, { status: 'busy', decision: 'rejected', rejectReason: trimmed });
      try {
        await rejectOne(baseUrl, id, trimmed);
        setRowState(id, { status: 'done' });
        nOk += 1;
      } catch (err) {
        setRowState(id, {
          status: 'error',
          error: err instanceof Error ? err.message : 'Reject failed.',
        });
        nErr += 1;
      }
    }
    setBulkBusy(false);
    setBulkSummary(`Rejected ${nOk} · Failed ${nErr}`);
  }, [baseUrl, liveRows, setRowState]);

  // ── Group cards ────────────────────────────────────────────────────────
  const groupedCards = useMemo(() => {
    const map = new Map<string, VirtualCard[]>();
    for (const card of cards) {
      const arr = map.get(card.category) ?? [];
      arr.push(card);
      map.set(card.category, arr);
    }
    // Stable order per GROUP_ORDER, then any unexpected categories last.
    const ordered: Array<[string, VirtualCard[]]> = [];
    for (const cat of GROUP_ORDER) {
      const arr = map.get(cat);
      if (arr && arr.length > 0) ordered.push([cat, arr]);
    }
    for (const [cat, arr] of map.entries()) {
      if (!GROUP_ORDER.includes(cat)) ordered.push([cat, arr]);
    }
    return ordered;
  }, [cards]);

  const totalCitationPages = useMemo(() => {
    if (cardCitations.length === 0) return 1;
    return Math.max(...cardCitations.map((c) => c.page));
  }, [cardCitations]);

  // Row count drives the action-bar progress + bulk button counter
  // (one decision per staging row, even when a row maps to N cards).
  const allCount = rows.length;

  // Patient name resolution — fetch once on mount; falls back to the
  // raw patient_id if FHIR is unavailable. Without this, the rail
  // subtitle showed the OpenEMR numeric pid (e.g. "18") which means
  // nothing to a clinician glancing at the panel.
  const [patientName, setPatientName] = useState<string | null>(null);
  useEffect(() => {
    if (!patientId || !baseUrl) return;
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetch(
          `${baseUrl}/fhir/patient/${encodeURIComponent(patientId)}`,
        );
        if (!res.ok) return;
        const data = (await res.json()) as {
          name?: Array<{ given?: string[]; family?: string }>;
        };
        const n0 = data.name?.[0];
        if (!n0) return;
        const joined = [...(n0.given ?? []), n0.family ?? '']
          .filter((s) => typeof s === 'string' && s.trim().length > 0)
          .join(' ')
          .trim();
        if (!cancelled && joined) setPatientName(joined);
      } catch {
        // best-effort
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [baseUrl, patientId]);

  return (
    <div className="copilot-doc-review" style={rootStyle}>
      <style>{REVIEW_CSS}</style>

      <header className="cdr-topbar">
        <div className="cdr-topbar-left">
          <nav className="cdr-breadcrumb" aria-label="Breadcrumb">
            <button
              type="button"
              className="cdr-breadcrumb-link cdr-breadcrumb-back"
              onClick={onClose}
              aria-label="Back to inbox"
              title="Back to inbox"
            >
              <BackIcon />
            </button>
            <button
              type="button"
              className="cdr-breadcrumb-link"
              onClick={onClose}
            >
              Document Inbox
            </button>
            <ChevronIcon />
            <span className="cdr-breadcrumb-current">
              {fileBatchId ? `Batch ${_truncate(fileBatchId, 12)}` : 'Document'}
            </span>
          </nav>
          <span className="cdr-doc-meta">
            {_truncate(documentReferenceId, 24)} · p{activePage}/{Math.max(activePage, totalCitationPages)}
          </span>
        </div>

        <div className="cdr-topbar-right">
          <span className={`cdr-status-pill${readOnly ? ' cdr-status-pill-readonly' : ''}`}>
            {readOnly
              ? 'Read-only · Approved'
              : reviewedCount === allCount && allCount > 0
                ? 'All Reviewed'
                : 'Ready for Review'}
          </span>
          <button
            type="button"
            className="cdr-btn cdr-btn-icon"
            title="Close review"
            onClick={onClose}
            aria-label="Close"
          >
            <CloseIcon />
          </button>
        </div>
      </header>

      <div className="cdr-layout">
        <DocViewer
          previewKind={previewKind}
          previewLoading={previewLoading}
          previewError={previewError}
          previewBytes={previewBytes}
          imageBlobUrl={imageBlobUrl}
          synthesizedParagraphs={_synthesizeParagraphs(rows)}
          docxParagraphs={docxParagraphs}
          activePage={activePage}
          setActivePage={setActivePage}
          zoom={zoom}
          setZoom={setZoom}
          activeCardId={activeCardId}
          activePageCitations={activePageCitations}
          onBboxClick={onBboxClick}
          totalPages={totalCitationPages}
          editMode={editMode}
          setEditMode={setEditMode}
          editedBboxes={editedBboxes}
          onBboxOptimistic={onBboxOptimistic}
          onBboxCommit={onBboxCommit}
          readOnly={readOnly}
          creatingMode={creatingMode}
          setCreatingMode={setCreatingMode}
          onCreateDraftFromDrag={onCreateDraftFromDrag}
        />

        <aside className="cdr-rail">
          <div className="cdr-rail-header">
            <div className="cdr-rail-title">Extracted Fields</div>
            <div className="cdr-rail-subtitle">
              <span>{cards.length} field{cards.length === 1 ? '' : 's'}</span>
              <span className="cdr-dot" />
              <span>{patientName || (patientId ? `Patient #${_truncate(patientId, 8)}` : 'Patient')}</span>
              <span className="cdr-dot" />
              <span>Intake Form</span>
            </div>
          </div>

          <div className="cdr-field-list">
            {loading && <div className="cdr-state-msg">Loading staged rows…</div>}
            {loadError && (
              <div role="alert" className="cdr-state-msg cdr-state-err">{loadError}</div>
            )}
            {!loading && !loadError && rows.length === 0 && (
              <div className="cdr-state-msg">No staged rows for this document.</div>
            )}
            {groupedCards.map(([category, items]) => (
              <FieldGroup key={category} title={category}>
                {items.map((card) => (
                  <FieldCard
                    key={card.id}
                    card={card}
                    active={activeCardId === card.id}
                    busy={bulkBusy}
                    readOnly={readOnly}
                    onClick={() => onFieldClick(card.id)}
                    onValueChange={onValueChange}
                    onApprove={() => void onApproveOne(card.parent.row.id)}
                    onReject={() => void onRejectOne(card.parent.row.id)}
                  />
                ))}
              </FieldGroup>
            ))}
          </div>

          {!readOnly && (
            <div className="cdr-action-bar">
              <div className="cdr-progress">
                <div className="cdr-progress-bar">
                  <div
                    className="cdr-progress-fill"
                    style={{
                      width: `${allCount === 0 ? 0 : (reviewedCount / allCount) * 100}%`,
                    }}
                  />
                </div>
                <div className="cdr-progress-text">
                  <strong>{reviewedCount}</strong> of {allCount} reviewed
                  {bulkSummary ? ` · ${bulkSummary}` : ''}
                </div>
              </div>
              <div className="cdr-action-buttons">
                <button
                  type="button"
                  className="cdr-btn cdr-btn-action-reject"
                  onClick={() => void onRejectAll()}
                  disabled={bulkBusy || liveRows.length === 0}
                >
                  Reject all
                </button>
                <button
                  type="button"
                  className="cdr-btn cdr-btn-action-approve"
                  onClick={() => void onApproveAll()}
                  disabled={bulkBusy || liveRows.length === 0}
                >
                  {bulkBusy ? 'Working…' : 'Approve all'}
                  <span className="cdr-count">{liveRows.length}</span>
                </button>
              </div>
            </div>
          )}
        </aside>
      </div>

      {creatingDraft && (
        <ManualCreateForm
          draft={creatingDraft}
          onCancel={onCreateCancel}
          onSubmit={onCreateSubmit}
        />
      )}
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// DocViewer — left pane: PDF / image / DOCX paragraphs + bbox overlays
// ────────────────────────────────────────────────────────────────────────────

interface DocViewerProps {
  previewKind: ViewerKind;
  previewLoading: boolean;
  previewError: string | null;
  previewBytes: ArrayBuffer | null;
  imageBlobUrl: string | null;
  synthesizedParagraphs: string[];
  docxParagraphs: DocxParagraphPayload[] | null;
  activePage: number;
  setActivePage: (n: number) => void;
  zoom: number;
  setZoom: (n: number) => void;
  activeCardId: string | null;
  activePageCitations: FlatCitation[];
  onBboxClick: (cardId: string) => void;
  totalPages: number;
  editMode: boolean;
  setEditMode: (next: boolean) => void;
  editedBboxes: Record<string, [number, number, number, number]>;
  onBboxOptimistic: (
    cardId: string,
    bbox: [number, number, number, number],
  ) => void;
  onBboxCommit: (
    cardId: string,
    newBbox: [number, number, number, number],
    before: [number, number, number, number],
    page: number,
    deltaPx: { dx: number; dy: number; dw: number; dh: number },
  ) => Promise<void>;
  readOnly: boolean;
  creatingMode: boolean;
  setCreatingMode: (next: boolean) => void;
  onCreateDraftFromDrag: (
    bbox: [number, number, number, number],
    page: number,
  ) => void;
}

interface PageGeometry {
  /** Rendered canvas/image size in CSS px (post-zoom). */
  renderWidth: number;
  renderHeight: number;
  /** Intrinsic source size — PDF points or natural image px. */
  sourceWidth: number;
  sourceHeight: number;
}

function DocViewer(p: DocViewerProps): ReactElement {
  const {
    previewKind,
    previewLoading,
    previewError,
    previewBytes,
    imageBlobUrl,
    synthesizedParagraphs,
    docxParagraphs,
    activePage,
    setActivePage,
    zoom,
    setZoom,
    activeCardId,
    activePageCitations,
    onBboxClick,
    totalPages,
    editMode,
    setEditMode,
    editedBboxes,
    onBboxOptimistic,
    onBboxCommit,
    readOnly,
    creatingMode,
    setCreatingMode,
    onCreateDraftFromDrag,
  } = p;

  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);

  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [pdfLoadError, setPdfLoadError] = useState<string | null>(null);
  const [pageGeom, setPageGeom] = useState<PageGeometry | null>(null);
  const [pdfPageCount, setPdfPageCount] = useState<number>(0);

  // Load PDF when bytes land + kind is PDF.
  useEffect(() => {
    if (previewKind !== 'pdf' || !previewBytes) {
      setPdf(null);
      setPageGeom(null);
      setPdfLoadError(null);
      return;
    }
    let cancelled = false;
    let source: ArrayBuffer | undefined;
    try {
      source = previewBytes.slice(0);
    } catch {
      source = undefined;
    }
    if (!source) {
      setPdfLoadError('Could not read document bytes.');
      return;
    }
    setPdfLoadError(null);
    loadPdf(source)
      .then((doc) => {
        if (cancelled) return;
        setPdf(doc);
        setPdfPageCount(doc.numPages);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        console.error('[DocumentReviewPanel] PDF load failed', err);
        setPdfLoadError('Could not load PDF.');
      });
    return () => {
      cancelled = true;
    };
  }, [previewBytes, previewKind]);

  // Render the active PDF page.
  useEffect(() => {
    if (!pdf || !canvasRef.current || previewKind !== 'pdf') return;
    let cancelled = false;
    const canvas = canvasRef.current;
    const renderPage = async (): Promise<void> => {
      const safePage = Math.min(Math.max(1, activePage), pdf.numPages);
      const page = await pdf.getPage(safePage);
      const baseViewport = page.getViewport({ scale: 1 });
      const desiredWidth = Math.min(680, baseViewport.width * 1.5);
      const baseScale = desiredWidth / baseViewport.width;
      const scale = baseScale * zoom;
      const viewport = page.getViewport({ scale });
      const ctx = canvas.getContext('2d');
      if (!ctx || cancelled) return;
      canvas.width = Math.floor(viewport.width);
      canvas.height = Math.floor(viewport.height);
      await page.render({ canvasContext: ctx, viewport }).promise;
      if (cancelled) return;
      setPageGeom({
        renderWidth: canvas.width,
        renderHeight: canvas.height,
        sourceWidth: baseViewport.width,
        sourceHeight: baseViewport.height,
      });
    };
    void renderPage().catch((err: unknown) => {
      if (cancelled) return;
      console.error('[DocumentReviewPanel] page render failed', err);
      setPdfLoadError('Could not render page.');
    });
    return () => {
      cancelled = true;
    };
  }, [pdf, activePage, zoom, previewKind]);

  // Page bounds: PDFs use pdf.numPages; everything else is single-page.
  const pageCount = previewKind === 'pdf' ? pdfPageCount || totalPages : 1;
  const canPrev = activePage > 1;
  const canNext = activePage < pageCount;

  const onZoomIn = (): void => setZoom(Math.min(zoom + 0.1, 2.0));
  const onZoomOut = (): void => setZoom(Math.max(zoom - 0.1, 0.5));

  return (
    <section className="cdr-doc-viewer">
      <div className="cdr-doc-controls">
        <div className="cdr-doc-controls-nav">
          <button
            type="button"
            className="cdr-btn cdr-btn-ghost cdr-btn-icon"
            onClick={() => setActivePage(Math.max(1, activePage - 1))}
            disabled={!canPrev}
            title="Previous page"
            aria-label="Previous page"
          >
            <ChevLeftIcon />
          </button>
          <span className="cdr-page-info">
            PAGE {activePage} OF {pageCount}
          </span>
          <button
            type="button"
            className="cdr-btn cdr-btn-ghost cdr-btn-icon"
            onClick={() => setActivePage(Math.min(pageCount, activePage + 1))}
            disabled={!canNext}
            title="Next page"
            aria-label="Next page"
          >
            <ChevRightIcon />
          </button>
        </div>
        <div className="cdr-doc-controls-nav">
          <button
            type="button"
            className="cdr-btn cdr-btn-ghost cdr-btn-icon"
            onClick={onZoomOut}
            disabled={zoom <= 0.5}
            title="Zoom out"
            aria-label="Zoom out"
          >
            <ZoomOutIcon />
          </button>
          <span className="cdr-page-info">{Math.round(zoom * 100)}%</span>
          <button
            type="button"
            className="cdr-btn cdr-btn-ghost cdr-btn-icon"
            onClick={onZoomIn}
            disabled={zoom >= 2.0}
            title="Zoom in"
            aria-label="Zoom in"
          >
            <ZoomInIcon />
          </button>
          {!readOnly && (
            <>
              <button
                type="button"
                className={`cdr-btn cdr-btn-edit-toggle${editMode ? ' cdr-btn-edit-toggle-active' : ''}`}
                onClick={() => {
                  setEditMode(!editMode);
                  setCreatingMode(false);
                }}
                title="Reshape citation boxes (E)"
                aria-pressed={editMode}
              >
                {editMode ? 'Done' : 'Edit boxes'}
              </button>
              <button
                type="button"
                className={`cdr-btn cdr-btn-create-toggle${creatingMode ? ' cdr-btn-create-toggle-active' : ''}`}
                onClick={() => {
                  setCreatingMode(!creatingMode);
                  setEditMode(false);
                }}
                title="Add a manual citation"
                aria-pressed={creatingMode}
              >
                {creatingMode ? 'Cancel' : '+ Add field'}
              </button>
            </>
          )}
        </div>
      </div>

      <div className="cdr-doc-stage">
        {previewLoading && <div className="cdr-state-msg">Loading source preview…</div>}
        {previewError && (
          <div role="alert" className="cdr-state-msg">
            Source preview unavailable; data still editable on the right.
          </div>
        )}

        {previewKind === 'pdf' && !previewLoading && !previewError && (
          <div className="cdr-doc-page-wrap">
            {pdfLoadError && (
              <div role="alert" className="cdr-state-msg">{pdfLoadError}</div>
            )}
            <div className="cdr-doc-page" style={pageGeom ? { width: pageGeom.renderWidth } : undefined}>
              <canvas ref={canvasRef} className="cdr-doc-canvas" />
              {pageGeom && (
                <BboxLayer
                  geom={pageGeom}
                  citations={activePageCitations}
                  activeCardId={activeCardId}
                  onBboxClick={onBboxClick}
                  editMode={editMode}
                  editedBboxes={editedBboxes}
                  onBboxOptimistic={onBboxOptimistic}
                  onBboxCommit={onBboxCommit}
                />
              )}
              {pageGeom && creatingMode && (
                <CreateLayer
                  geom={pageGeom}
                  page={activePage}
                  onCreated={onCreateDraftFromDrag}
                />
              )}
            </div>
          </div>
        )}

        {previewKind === 'image' && !previewLoading && !previewError && imageBlobUrl && (
          <div className="cdr-doc-page-wrap">
            <div className="cdr-doc-page cdr-doc-page-image">
              <img
                ref={imgRef}
                src={imageBlobUrl}
                alt="Source document"
                className="cdr-doc-image"
                style={{ width: `${100 * zoom}%` }}
                onLoad={(e) => {
                  const el = e.currentTarget;
                  setPageGeom({
                    renderWidth: el.clientWidth,
                    renderHeight: el.clientHeight,
                    sourceWidth: el.naturalWidth,
                    sourceHeight: el.naturalHeight,
                  });
                }}
              />
              {pageGeom && (
                <BboxLayer
                  geom={pageGeom}
                  citations={activePageCitations}
                  activeCardId={activeCardId}
                  onBboxClick={onBboxClick}
                  editMode={editMode}
                  editedBboxes={editedBboxes}
                  onBboxOptimistic={onBboxOptimistic}
                  onBboxCommit={onBboxCommit}
                />
              )}
              {pageGeom && creatingMode && (
                <CreateLayer
                  geom={pageGeom}
                  page={activePage}
                  onCreated={onCreateDraftFromDrag}
                />
              )}
            </div>
          </div>
        )}

        {previewKind === 'docx' && !previewLoading && !previewError && (() => {
          // Prefer the real DOCX paragraph list (full letter body) when the
          // backend has returned it; fall back to the citation-quote synth
          // while loading or on fetch failure so the preview is never blank.
          const useReal = docxParagraphs != null && docxParagraphs.length > 0;
          const paragraphs = useReal
            ? docxParagraphs!.map((p) => p.text)
            : synthesizedParagraphs;
          let activeIndex: number | null = null;
          if (activeCardId != null) {
            const activeCit = activePageCitations.find((c) => c.cardId === activeCardId);
            if (useReal && activeCit?.paraIdx != null) {
              // paraIdx is 1-based in the loader contract; docxParagraphs
              // entries are also document-order, so subtract 1.
              const idx = docxParagraphs!.findIndex((p) => p.para_idx === activeCit.paraIdx);
              activeIndex = idx >= 0 ? idx : null;
            } else if (!useReal) {
              const idx = activePageCitations.findIndex((c) => c.cardId === activeCardId);
              activeIndex = idx >= 0 ? idx : null;
            }
          }
          return (
            <div className="cdr-doc-page-wrap">
              <div className="cdr-doc-page cdr-doc-page-docx">
                <DocxParagraphList
                  paragraphs={paragraphs}
                  activeIndex={activeIndex}
                  onClick={(idx) => {
                    if (useReal) {
                      // Map docx paragraph index → citation whose paraIdx
                      // matches; surface its card if any.
                      const para = docxParagraphs![idx];
                      if (!para) return;
                      const target = activePageCitations.find(
                        (c) => c.paraIdx === para.para_idx,
                      );
                      if (target) onBboxClick(target.cardId);
                    } else {
                      const target = activePageCitations[idx];
                      if (target) onBboxClick(target.cardId);
                    }
                  }}
                />
              </div>
            </div>
          );
        })()}

        {previewKind === 'unknown' && !previewLoading && previewBytes && (
          <div className="cdr-state-msg">
            Source format not previewable; citations remain available in the rail.
          </div>
        )}
      </div>
    </section>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// BboxLayer — draws every citation on the active page; active one is amber
// ────────────────────────────────────────────────────────────────────────────

interface BboxLayerProps {
  geom: PageGeometry;
  citations: FlatCitation[];
  activeCardId: string | null;
  onBboxClick: (cardId: string) => void;
  editMode: boolean;
  editedBboxes: Record<string, [number, number, number, number]>;
  onBboxOptimistic: (
    cardId: string,
    bbox: [number, number, number, number],
  ) => void;
  onBboxCommit: (
    cardId: string,
    newBbox: [number, number, number, number],
    before: [number, number, number, number],
    page: number,
    deltaPx: { dx: number; dy: number; dw: number; dh: number },
  ) => Promise<void>;
}

const _BBOX_MIN_PT = 5.0;

type _Handle = 'nw' | 'n' | 'ne' | 'e' | 'se' | 's' | 'sw' | 'w' | 'body';

function BboxLayer(p: BboxLayerProps): ReactElement {
  const {
    geom,
    citations,
    activeCardId,
    onBboxClick,
    editMode,
    editedBboxes,
    onBboxOptimistic,
    onBboxCommit,
  } = p;
  const sx = geom.renderWidth / geom.sourceWidth;
  const sy = geom.renderHeight / geom.sourceHeight;
  return (
    <div
      className={`cdr-bbox-layer${editMode ? ' cdr-bbox-layer-edit' : ''}`}
      aria-hidden="false"
    >
      {citations.map((c) => {
        const effective = editedBboxes[c.cardId] ?? c.bbox;
        if (!effective) return null;
        return (
          <CitationBox
            key={c.cardId}
            citation={c}
            bbox={effective}
            sx={sx}
            sy={sy}
            active={c.cardId === activeCardId}
            editMode={editMode}
            onClick={() => onBboxClick(c.cardId)}
            onOptimistic={onBboxOptimistic}
            onCommit={onBboxCommit}
          />
        );
      })}
    </div>
  );
}

interface CitationBoxProps {
  citation: FlatCitation;
  bbox: [number, number, number, number];
  sx: number;
  sy: number;
  active: boolean;
  editMode: boolean;
  onClick: () => void;
  onOptimistic: (
    cardId: string,
    bbox: [number, number, number, number],
  ) => void;
  onCommit: (
    cardId: string,
    newBbox: [number, number, number, number],
    before: [number, number, number, number],
    page: number,
    deltaPx: { dx: number; dy: number; dw: number; dh: number },
  ) => Promise<void>;
}

function CitationBox(p: CitationBoxProps): ReactElement {
  const { citation, bbox, sx, sy, active, editMode, onClick, onOptimistic, onCommit } = p;
  const dragRef = useRef<{
    handle: _Handle;
    startMouseX: number;
    startMouseY: number;
    startBbox: [number, number, number, number];
    committedBefore: [number, number, number, number];
  } | null>(null);

  const [x, y, w, h] = bbox;
  const left = x * sx;
  const top = y * sy;
  const width = w * sx;
  const height = h * sy;

  const handlePointerDown = useCallback(
    (handle: _Handle) =>
      (e: React.PointerEvent<HTMLDivElement>) => {
        if (!editMode) return;
        e.preventDefault();
        e.stopPropagation();
        (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
        dragRef.current = {
          handle,
          startMouseX: e.clientX,
          startMouseY: e.clientY,
          startBbox: [...bbox],
          committedBefore: [...bbox],
        };
      },
    [editMode, bbox],
  );

  const handlePointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const drag = dragRef.current;
      if (!drag) return;
      const dxPx = e.clientX - drag.startMouseX;
      const dyPx = e.clientY - drag.startMouseY;
      const dxPdf = dxPx / sx;
      const dyPdf = dyPx / sy;
      const [sx0, sy0, sw0, sh0] = drag.startBbox;
      let nx = sx0,
        ny = sy0,
        nw = sw0,
        nh = sh0;
      switch (drag.handle) {
        case 'body':
          nx = sx0 + dxPdf;
          ny = sy0 + dyPdf;
          break;
        case 'nw':
          nx = sx0 + dxPdf;
          ny = sy0 + dyPdf;
          nw = sw0 - dxPdf;
          nh = sh0 - dyPdf;
          break;
        case 'n':
          ny = sy0 + dyPdf;
          nh = sh0 - dyPdf;
          break;
        case 'ne':
          ny = sy0 + dyPdf;
          nw = sw0 + dxPdf;
          nh = sh0 - dyPdf;
          break;
        case 'e':
          nw = sw0 + dxPdf;
          break;
        case 'se':
          nw = sw0 + dxPdf;
          nh = sh0 + dyPdf;
          break;
        case 's':
          nh = sh0 + dyPdf;
          break;
        case 'sw':
          nx = sx0 + dxPdf;
          nw = sw0 - dxPdf;
          nh = sh0 + dyPdf;
          break;
        case 'w':
          nx = sx0 + dxPdf;
          nw = sw0 - dxPdf;
          break;
      }
      // Constrain min size + non-negative origin.
      if (nw < _BBOX_MIN_PT) {
        if (drag.handle === 'nw' || drag.handle === 'sw' || drag.handle === 'w') {
          nx = sx0 + sw0 - _BBOX_MIN_PT;
        }
        nw = _BBOX_MIN_PT;
      }
      if (nh < _BBOX_MIN_PT) {
        if (drag.handle === 'nw' || drag.handle === 'ne' || drag.handle === 'n') {
          ny = sy0 + sh0 - _BBOX_MIN_PT;
        }
        nh = _BBOX_MIN_PT;
      }
      if (nx < 0) {
        if (drag.handle !== 'body') nw += nx;
        nx = 0;
      }
      if (ny < 0) {
        if (drag.handle !== 'body') nh += ny;
        ny = 0;
      }
      const next: [number, number, number, number] = [
        Number(nx.toFixed(3)),
        Number(ny.toFixed(3)),
        Number(nw.toFixed(3)),
        Number(nh.toFixed(3)),
      ];
      onOptimistic(citation.cardId, next);
    },
    [sx, sy, onOptimistic, citation.cardId],
  );

  const handlePointerUp = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const drag = dragRef.current;
      dragRef.current = null;
      if (!drag) return;
      (e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId);
      const before = drag.committedBefore;
      // bbox at this point is the latest optimistic state (parent state).
      // Skip a no-op commit when nothing changed.
      if (
        before[0] === bbox[0] &&
        before[1] === bbox[1] &&
        before[2] === bbox[2] &&
        before[3] === bbox[3]
      ) {
        return;
      }
      void onCommit(
        citation.cardId,
        bbox,
        before,
        citation.page,
        {
          dx: Math.round(e.clientX - drag.startMouseX),
          dy: Math.round(e.clientY - drag.startMouseY),
          dw: Number(((bbox[2] - before[2]) * sx).toFixed(1)),
          dh: Number(((bbox[3] - before[3]) * sy).toFixed(1)),
        },
      );
    },
    [bbox, sx, sy, onCommit, citation.cardId, citation.page],
  );

  return (
    <div
      className={[
        'cdr-citation-box',
        active ? 'cdr-citation-box-active' : '',
        editMode ? 'cdr-citation-box-edit' : '',
      ].filter(Boolean).join(' ')}
      style={{ left, top, width, height }}
      onClick={(e) => {
        if (editMode) return;
        e.stopPropagation();
        onClick();
      }}
      onPointerDown={editMode ? handlePointerDown('body') : undefined}
      onPointerMove={editMode ? handlePointerMove : undefined}
      onPointerUp={editMode ? handlePointerUp : undefined}
      title={citation.quote || citation.shortLabel}
    >
      <span
        className="cdr-citation-badge"
        data-ordinal={citation.ordinal}
      >
        <span className="cdr-citation-badge-number">{citation.ordinal}</span>
        <span className="cdr-citation-badge-detail">
          {' '}
          {citation.shortLabel}
        </span>
      </span>
      {editMode && (
        <>
          {(['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'] as const).map((h) => (
            <div
              key={h}
              className={`cdr-bbox-handle cdr-bbox-handle-${h}`}
              onPointerDown={handlePointerDown(h)}
              onPointerMove={handlePointerMove}
              onPointerUp={handlePointerUp}
              role="presentation"
            />
          ))}
        </>
      )}
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// CreateLayer — drag-to-draw a new bbox for a manual citation
// ────────────────────────────────────────────────────────────────────────────

interface CreateLayerProps {
  geom: PageGeometry;
  page: number;
  onCreated: (
    bbox: [number, number, number, number],
    page: number,
  ) => void;
}

function CreateLayer(p: CreateLayerProps): ReactElement {
  const { geom, page, onCreated } = p;
  const sx = geom.renderWidth / geom.sourceWidth;
  const sy = geom.renderHeight / geom.sourceHeight;
  // Internal CSS-pixel rect tracked from pointer events. We convert to
  // PDF points only on commit; intermediate frames stay in canvas-px
  // for cheap rendering.
  const [draft, setDraft] = useState<{
    sxp: number;
    syp: number;
    expx: number;
    eypx: number;
  } | null>(null);

  const layerRef = useRef<HTMLDivElement | null>(null);

  const localFromEvent = (e: React.PointerEvent<HTMLDivElement>): { x: number; y: number } | null => {
    const layer = layerRef.current;
    if (!layer) return null;
    const r = layer.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  const onDown = (e: React.PointerEvent<HTMLDivElement>): void => {
    e.preventDefault();
    const local = localFromEvent(e);
    if (!local) return;
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
    setDraft({ sxp: local.x, syp: local.y, expx: local.x, eypx: local.y });
  };

  const onMove = (e: React.PointerEvent<HTMLDivElement>): void => {
    if (!draft) return;
    const local = localFromEvent(e);
    if (!local) return;
    setDraft({ ...draft, expx: local.x, eypx: local.y });
  };

  const onUp = (e: React.PointerEvent<HTMLDivElement>): void => {
    if (!draft) return;
    (e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId);
    const xPx = Math.min(draft.sxp, draft.expx);
    const yPx = Math.min(draft.syp, draft.eypx);
    const wPx = Math.abs(draft.expx - draft.sxp);
    const hPx = Math.abs(draft.eypx - draft.syp);
    setDraft(null);
    // Convert px → PDF points; bail if the rectangle is below the
    // backend's 5pt floor so the operator doesn't get a 400.
    const xPt = xPx / sx;
    const yPt = yPx / sy;
    const wPt = wPx / sx;
    const hPt = hPx / sy;
    if (wPt < _BBOX_MIN_PT || hPt < _BBOX_MIN_PT) return;
    onCreated(
      [
        Number(xPt.toFixed(3)),
        Number(yPt.toFixed(3)),
        Number(wPt.toFixed(3)),
        Number(hPt.toFixed(3)),
      ],
      page,
    );
  };

  // Live preview rect (in CSS px relative to the doc-page).
  const previewLeft = draft ? Math.min(draft.sxp, draft.expx) : 0;
  const previewTop = draft ? Math.min(draft.syp, draft.eypx) : 0;
  const previewW = draft ? Math.abs(draft.expx - draft.sxp) : 0;
  const previewH = draft ? Math.abs(draft.eypx - draft.syp) : 0;

  return (
    <div
      ref={layerRef}
      className="cdr-create-layer"
      onPointerDown={onDown}
      onPointerMove={onMove}
      onPointerUp={onUp}
      role="presentation"
    >
      {draft && previewW > 0 && previewH > 0 && (
        <div
          className="cdr-create-preview"
          style={{
            left: previewLeft,
            top: previewTop,
            width: previewW,
            height: previewH,
          }}
        />
      )}
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// ManualCreateForm — modal that captures kind + value for a hand-drawn box
// ────────────────────────────────────────────────────────────────────────────

interface ManualCreateFormProps {
  draft: { bbox: [number, number, number, number]; page: number };
  onCancel: () => void;
  onSubmit: (form: {
    kind: ManualCitationKind;
    value: string;
    extra?: string;
  }) => Promise<void>;
}

function ManualCreateForm(p: ManualCreateFormProps): ReactElement {
  const { draft, onCancel, onSubmit } = p;
  const [kind, setKind] = useState<ManualCitationKind>('medication');
  const [value, setValue] = useState('');
  const [extra, setExtra] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const extraLabel = (() => {
    switch (kind) {
      case 'medication': return 'Dose (optional)';
      case 'allergy': return 'Reaction (optional)';
      case 'family_history': return 'Relation (Father / Mother / Sibling)';
      case 'other': return 'Note (optional)';
      default: return '';
    }
  })();
  const showExtra = extraLabel !== '';
  const valueLabel = (() => {
    switch (kind) {
      case 'medication': return 'Medication name';
      case 'allergy': return 'Substance';
      case 'family_history': return 'Condition';
      case 'chief_concern': return 'Reason for visit';
      case 'code_status': return 'Code status';
      case 'other': return 'Description';
    }
  })();

  const submit = async (e: React.FormEvent<HTMLFormElement>): Promise<void> => {
    e.preventDefault();
    setError(null);
    if (!value.trim()) {
      setError('Please enter a value.');
      return;
    }
    if (kind === 'family_history' && !extra.trim()) {
      setError('Family history rows need a relation (Father, Mother, Sibling, …).');
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit({
        kind,
        value: value.trim(),
        extra: extra.trim() || undefined,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Create failed.');
      setSubmitting(false);
      return;
    }
    setSubmitting(false);
  };

  return (
    <div
      className="cdr-modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label="Add manual citation"
      onClick={(e) => {
        if (e.target === e.currentTarget && !submitting) onCancel();
      }}
    >
      <form className="cdr-modal" onSubmit={submit}>
        <div className="cdr-modal-header">
          <span className="cdr-modal-title">Add manual citation</span>
          <button
            type="button"
            className="cdr-btn cdr-btn-ghost cdr-btn-icon"
            onClick={onCancel}
            disabled={submitting}
            aria-label="Cancel"
          >
            <CloseIcon />
          </button>
        </div>
        <div className="cdr-modal-meta">
          Page {draft.page} · {Math.round(draft.bbox[2])} × {Math.round(draft.bbox[3])} pt
          at ({Math.round(draft.bbox[0])}, {Math.round(draft.bbox[1])})
        </div>

        <div className="cdr-modal-body">
          <label className="cdr-modal-label">
            <span>Field type</span>
            <select
              className="cdr-field-input"
              value={kind}
              disabled={submitting}
              onChange={(e) => setKind(e.target.value as ManualCitationKind)}
            >
              <option value="medication">Medication</option>
              <option value="allergy">Allergy</option>
              <option value="family_history">Family history</option>
              <option value="chief_concern">Chief concern</option>
              <option value="code_status">Code status</option>
              <option value="other">Other / Custom</option>
            </select>
          </label>

          <label className="cdr-modal-label">
            <span>{valueLabel}</span>
            {kind === 'chief_concern' || kind === 'other' ? (
              <textarea
                className="cdr-field-input"
                value={value}
                disabled={submitting}
                rows={3}
                autoFocus
                onChange={(e) => setValue(e.target.value)}
              />
            ) : kind === 'code_status' ? (
              <select
                className="cdr-field-input"
                value={value || 'full_code'}
                disabled={submitting}
                onChange={(e) => setValue(e.target.value)}
              >
                <option value="full_code">full_code</option>
                <option value="DNR">DNR</option>
                <option value="DNI">DNI</option>
                <option value="comfort_care">comfort_care</option>
                <option value="POLST">POLST</option>
                <option value="unknown">unknown</option>
              </select>
            ) : (
              <input
                type="text"
                className="cdr-field-input"
                value={value}
                disabled={submitting}
                autoFocus
                onChange={(e) => setValue(e.target.value)}
              />
            )}
          </label>

          {showExtra && (
            <label className="cdr-modal-label">
              <span>{extraLabel}</span>
              <input
                type="text"
                className="cdr-field-input"
                value={extra}
                disabled={submitting}
                onChange={(e) => setExtra(e.target.value)}
              />
            </label>
          )}

          {error && <div role="alert" className="cdr-modal-error">{error}</div>}
        </div>

        <div className="cdr-modal-footer">
          <button
            type="button"
            className="cdr-btn"
            onClick={onCancel}
            disabled={submitting}
          >
            Cancel
          </button>
          <button
            type="submit"
            className="cdr-btn cdr-btn-action-approve"
            disabled={submitting}
          >
            {submitting ? 'Saving…' : 'Add citation'}
          </button>
        </div>
      </form>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// DocxParagraphList — DOCX fallback inside the doc-page frame
// ────────────────────────────────────────────────────────────────────────────

interface DocxParagraphListProps {
  paragraphs: string[];
  activeIndex: number | null;
  onClick: (idx: number) => void;
}

function DocxParagraphList(p: DocxParagraphListProps): ReactElement {
  const refs = useRef<Array<HTMLDivElement | null>>([]);
  useEffect(() => {
    if (p.activeIndex == null || p.activeIndex < 0) return;
    const el = refs.current[p.activeIndex];
    if (el?.scrollIntoView) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }, [p.activeIndex]);
  if (p.paragraphs.length === 0) {
    return (
      <div className="cdr-docx-empty">
        Source preview unavailable; use field cards on the right for context.
      </div>
    );
  }
  return (
    <div className="cdr-docx-list">
      {p.paragraphs.map((text, idx) => {
        const active = idx === p.activeIndex;
        return (
          <div
            key={idx}
            ref={(el) => {
              refs.current[idx] = el;
            }}
            className={`cdr-docx-para${active ? ' cdr-docx-para-active' : ''}`}
            onClick={() => p.onClick(idx)}
          >
            {text || <em className="cdr-docx-empty-line">(empty)</em>}
          </div>
        );
      })}
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// FieldGroup + FieldCard
// ────────────────────────────────────────────────────────────────────────────

interface FieldGroupProps {
  title: string;
  children: ReactNode;
}

function FieldGroup(p: FieldGroupProps): ReactElement {
  return (
    <div className="cdr-field-group">
      <div className="cdr-field-group-title">{p.title}</div>
      {p.children}
    </div>
  );
}

interface FieldCardProps {
  card: VirtualCard;
  active: boolean;
  busy: boolean;
  readOnly?: boolean;
  onClick: () => void;
  onValueChange: (rowId: number, override: Record<string, unknown> | null) => void;
  onApprove: () => void;
  onReject: () => void;
}

function FieldCard(p: FieldCardProps): ReactElement {
  const { card, active, busy, readOnly = false, onClick, onValueChange, onApprove, onReject } = p;
  const lr = card.parent;
  const status = lr.state.status;
  const decision = lr.state.decision;
  const isManual = _isManualRow(lr.row);
  const cardClasses = [
    'cdr-field',
    active ? 'cdr-field-active' : '',
    status === 'done' && decision === 'approved' ? 'cdr-field-approved' : '',
    status === 'done' && decision === 'rejected' ? 'cdr-field-rejected' : '',
    status === 'error' ? 'cdr-field-error' : '',
    readOnly ? 'cdr-field-readonly' : '',
    isManual ? 'cdr-field-manual' : '',
  ].filter(Boolean).join(' ');

  const inputDisabled = readOnly || busy || status === 'busy' || status === 'done';

  const labelText = card.cardLabel;
  const citationChip = card.citation
    ? `p${card.citation.page} · #${card.citation.ordinal}`
    : '—';

  // Confidence is best-effort: payload may carry .confidence on the value
  // object (intake) or no signal at all. We only render the dot if we have
  // something to show — never fake it.
  const confidence = _confidenceFor(lr.row);

  return (
    <div
      id={`copilot-review-field-${card.id}`}
      className={cardClasses}
      onClick={(e) => {
        if ((e.target as HTMLElement).closest('input, textarea, select, button')) return;
        onClick();
      }}
    >
      <div className="cdr-field-row-1">
        <span className="cdr-field-label">{labelText}</span>
        {isManual && (
          <span className="cdr-field-manual-pill" title="Clinician-added citation">
            Manual
          </span>
        )}
        <span className="cdr-field-citation">{citationChip}</span>
      </div>

      {card.subfield != null
        ? <DemographicsSubfieldInput
            lr={lr}
            subfield={card.subfield}
            onValueChange={onValueChange}
            disabled={inputDisabled}
          />
        : <FieldEditor lr={lr} onValueChange={onValueChange} disabled={inputDisabled} />
      }

      <div className="cdr-field-actions">
        <div className="cdr-field-confidence">
          {confidence ? (
            <>
              <span className={`cdr-conf-dot cdr-conf-${confidence.level}`} />
              <span>{confidence.label}</span>
            </>
          ) : (
            <span className="cdr-conf-muted">
              {status === 'done'
                ? decision === 'approved' ? '✓ approved' : '✗ rejected'
                : status === 'error' ? `✗ ${lr.state.error ?? 'error'}` : ''}
            </span>
          )}
        </div>
        {!readOnly && (
          <div className="cdr-field-buttons">
            <button
              type="button"
              className="cdr-btn-tiny cdr-btn-tiny-reject"
              onClick={(e) => { e.stopPropagation(); onReject(); }}
              disabled={inputDisabled}
            >
              Reject
            </button>
            <button
              type="button"
              className="cdr-btn-tiny cdr-btn-tiny-approve"
              onClick={(e) => { e.stopPropagation(); onApprove(); }}
              disabled={inputDisabled}
            >
              Approve
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// FieldEditor — picks the right input shape per kind
// ────────────────────────────────────────────────────────────────────────────

interface FieldEditorInnerProps {
  lr: LoadedRow;
  onValueChange: (rowId: number, override: Record<string, unknown> | null) => void;
  disabled: boolean;
}

function FieldEditor(p: FieldEditorInnerProps): ReactElement {
  const kind = _kind(p.lr.row);
  switch (kind) {
    // Demographics is split per-subfield by the card builder; the
    // FieldCard renders DemographicsSubfieldInput directly. This
    // branch is unreachable in the current flow but kept for
    // defense in depth.
    case 'demographics': return <RawJsonInputs {...p} />;
    case 'chief_concern': return <ChiefConcernInputs {...p} />;
    case 'medication': return <MedicationInputs {...p} />;
    case 'allergy': return <AllergyInputs {...p} />;
    case 'family_history': return <FamilyHistoryInputs {...p} />;
    case 'code_status': return <CodeStatusInputs {...p} />;
    case 'lab': return <LabInputs {...p} />;
    case 'other': return <OtherInputs {...p} />;
    default: return <RawJsonInputs {...p} />;
  }
}

function OtherInputs(p: FieldEditorInnerProps): ReactElement {
  const payload = p.lr.row.payload as Record<string, unknown>;
  const initialValue = typeof payload.value === 'string' ? payload.value : '';
  const initialNote = typeof payload.note === 'string' ? payload.note : '';
  const [val, setVal] = useState(initialValue);
  const [note, setNote] = useState(initialNote);
  useEffect(() => {
    const next: Record<string, unknown> = { ...payload, value: val, note };
    p.onValueChange(p.lr.row.id, next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [val, note]);
  return (
    <>
      <textarea
        className="cdr-field-input"
        rows={2}
        value={val}
        placeholder="Description"
        disabled={p.disabled}
        onChange={(e) => setVal(e.target.value)}
      />
      <textarea
        className="cdr-field-input"
        rows={2}
        value={note}
        placeholder="Note (optional)"
        disabled={p.disabled}
        onChange={(e) => setNote(e.target.value)}
      />
    </>
  );
}

function _emitIntake(
  p: FieldEditorInnerProps,
  patches: Record<string, unknown>,
): void {
  const original = p.lr.row.payload;
  const oldValue = _payloadValue(p.lr.row);
  const merged: Record<string, unknown> = {
    ...original,
    value: { ...oldValue, ...patches },
  };
  p.onValueChange(p.lr.row.id, merged);
}

interface DemographicsSubfieldInputProps {
  lr: LoadedRow;
  subfield: 'name' | 'dob' | 'sex' | 'mrn' | 'address';
  onValueChange: (rowId: number, override: Record<string, unknown> | null) => void;
  disabled: boolean;
}

function DemographicsSubfieldInput(p: DemographicsSubfieldInputProps): ReactElement {
  const original = p.lr.row.payload as Record<string, unknown>;
  const demo = (original.demographics && typeof original.demographics === 'object'
    ? original.demographics as Record<string, unknown>
    : {}) as Record<string, unknown>;
  const sub = demo[p.subfield];
  const initial = typeof sub === 'string'
    ? sub
    : (sub && typeof sub === 'object' && typeof (sub as Record<string, unknown>).value === 'string'
        ? (sub as Record<string, unknown>).value as string
        : '');
  const [val, setVal] = useState(initial);
  // Preserve nested-citation shape on edit: write the new string into
  // demographics[sub].value and leave citations + neighbor fields alone.
  useEffect(() => {
    const next: Record<string, unknown> = { ...original };
    const nextDemo: Record<string, unknown> = { ...demo };
    if (sub && typeof sub === 'object' && !Array.isArray(sub)) {
      nextDemo[p.subfield] = { ...(sub as Record<string, unknown>), value: val };
    } else {
      nextDemo[p.subfield] = { value: val, citations: [], needs_review: false };
    }
    next.demographics = nextDemo;
    p.onValueChange(p.lr.row.id, next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [val, p.subfield, p.lr.row.id]);
  const placeholder = (() => {
    switch (p.subfield) {
      case 'name': return 'Patient name';
      case 'dob': return 'YYYY-MM-DD';
      case 'sex': return 'Sex';
      case 'mrn': return 'MRN';
      case 'address': return 'Address';
    }
  })();
  if (p.subfield === 'address') {
    return (
      <textarea
        className="cdr-field-input"
        rows={2}
        value={val}
        placeholder={placeholder}
        disabled={p.disabled}
        onChange={(e) => setVal(e.target.value)}
      />
    );
  }
  return (
    <input
      className="cdr-field-input"
      type={p.subfield === 'dob' ? 'date' : 'text'}
      value={val}
      placeholder={placeholder}
      disabled={p.disabled}
      onChange={(e) => setVal(e.target.value)}
    />
  );
}

function ChiefConcernInputs(p: FieldEditorInnerProps): ReactElement {
  const v = _payloadValue(p.lr.row);
  const [text, setText] = useState(_str(v.value) || _str(v.text) || _str(v.reason));
  useEffect(() => {
    _emitIntake(p, { value: text });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);
  return (
    <textarea className="cdr-field-input" rows={2} value={text} disabled={p.disabled}
      onChange={(e) => setText(e.target.value)} />
  );
}

function MedicationInputs(p: FieldEditorInnerProps): ReactElement {
  const v = _payloadValue(p.lr.row);
  const [name, setName] = useState(_str(v.name));
  const [dose, setDose] = useState(_str(v.dose));
  const [route, setRoute] = useState(_str(v.route) || _str(v.frequency));
  useEffect(() => {
    _emitIntake(p, { name, dose, route });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name, dose, route]);
  return (
    <>
      <input className="cdr-field-input cdr-field-input-compound" type="text" value={name}
        placeholder="Medication" disabled={p.disabled}
        onChange={(e) => setName(e.target.value)} />
      <div className="cdr-field-input-row">
        <input className="cdr-field-input" type="text" value={dose} placeholder="Dose"
          disabled={p.disabled} onChange={(e) => setDose(e.target.value)} />
        <input className="cdr-field-input" type="text" value={route} placeholder="Route/freq"
          disabled={p.disabled} onChange={(e) => setRoute(e.target.value)} />
      </div>
    </>
  );
}

function AllergyInputs(p: FieldEditorInnerProps): ReactElement {
  const v = _payloadValue(p.lr.row);
  const [substance, setSubstance] = useState(_str(v.substance));
  const [reaction, setReaction] = useState(_str(v.reaction));
  const [severity, setSeverity] = useState(_str(v.severity, 'unknown'));
  useEffect(() => {
    _emitIntake(p, { substance, reaction, severity });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [substance, reaction, severity]);
  return (
    <>
      <input className="cdr-field-input cdr-field-input-compound" type="text" value={substance}
        placeholder="Substance" disabled={p.disabled}
        onChange={(e) => setSubstance(e.target.value)} />
      <div className="cdr-field-input-row">
        <input className="cdr-field-input" type="text" value={reaction} placeholder="Reaction"
          disabled={p.disabled} onChange={(e) => setReaction(e.target.value)} />
        <select className="cdr-field-input" value={severity} disabled={p.disabled}
          onChange={(e) => setSeverity(e.target.value)}>
          <option value="mild">mild</option>
          <option value="moderate">moderate</option>
          <option value="severe">severe</option>
          <option value="life-threatening">life-threatening</option>
          <option value="unknown">unknown</option>
        </select>
      </div>
    </>
  );
}

function FamilyHistoryInputs(p: FieldEditorInnerProps): ReactElement {
  const v = _payloadValue(p.lr.row);
  const [relation, setRelation] = useState(_str(v.relation));
  const [condition, setCondition] = useState(_str(v.condition));
  const [ageAtOnset, setAgeAtOnset] = useState(_str(v.age_at_onset));
  const [status, setStatus] = useState(_str(v.status));
  const [snomed, setSnomed] = useState(_str(v.snomed_code));
  useEffect(() => {
    _emitIntake(p, {
      relation,
      condition,
      age_at_onset: ageAtOnset || null,
      status: status || null,
      snomed_code: snomed || null,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [relation, condition, ageAtOnset, status, snomed]);
  return (
    <>
      <input className="cdr-field-input cdr-field-input-compound" type="text" value={relation}
        placeholder="Relation (Father / Mother / Sibling)" disabled={p.disabled}
        onChange={(e) => setRelation(e.target.value)} />
      <input className="cdr-field-input cdr-field-input-compound" type="text" value={condition}
        placeholder="Condition" disabled={p.disabled}
        onChange={(e) => setCondition(e.target.value)} />
      <div className="cdr-field-input-row">
        <input className="cdr-field-input" type="text" value={ageAtOnset}
          placeholder="Age at onset" disabled={p.disabled}
          onChange={(e) => setAgeAtOnset(e.target.value)} />
        <input className="cdr-field-input" type="text" value={status}
          placeholder="Status (living / deceased age 72)" disabled={p.disabled}
          onChange={(e) => setStatus(e.target.value)} />
      </div>
      <input className="cdr-field-input" type="text" value={snomed}
        placeholder="SNOMED code" disabled={p.disabled}
        onChange={(e) => setSnomed(e.target.value)} />
    </>
  );
}

function CodeStatusInputs(p: FieldEditorInnerProps): ReactElement {
  const v = _payloadValue(p.lr.row);
  const [status, setStatusValue] = useState(_str(v.status, 'full'));
  useEffect(() => {
    _emitIntake(p, { status });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);
  return (
    <select className="cdr-field-input" value={status} disabled={p.disabled}
      onChange={(e) => setStatusValue(e.target.value)}>
      <option value="full">full</option>
      <option value="dnr">dnr</option>
      <option value="dni">dni</option>
      <option value="dnar">dnar</option>
      <option value="comfort_only">comfort_only</option>
    </select>
  );
}

function LabInputs(p: FieldEditorInnerProps): ReactElement {
  const payload = p.lr.row.payload as Record<string, unknown>;
  const codingArr = Array.isArray((payload.code as Record<string, unknown>)?.coding)
    ? ((payload.code as Record<string, unknown>).coding as Array<Record<string, unknown>>)
    : [];
  const coding0 = codingArr[0] ?? {};
  const valueQty = (payload.valueQuantity as Record<string, unknown>) ?? {};
  const [display, setDisplay] = useState(_str(coding0.display));
  const [value, setValue] = useState(_str(valueQty.value));
  const [unit, setUnit] = useState(_str(valueQty.unit));
  useEffect(() => {
    const next: Record<string, unknown> = JSON.parse(JSON.stringify(payload));
    const code = (next.code as Record<string, unknown>) ?? {};
    const codingArrNext = Array.isArray(code.coding) ? [...(code.coding as unknown[])] : [{}];
    codingArrNext[0] = { ...(codingArrNext[0] as Record<string, unknown>), display };
    next.code = { ...code, coding: codingArrNext };
    next.valueQuantity = {
      ...valueQty,
      value: value === '' ? null : Number(value),
      unit,
    };
    p.onValueChange(p.lr.row.id, next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [display, value, unit]);
  return (
    <>
      <input className="cdr-field-input cdr-field-input-compound" type="text" value={display}
        placeholder="Lab name" disabled={p.disabled}
        onChange={(e) => setDisplay(e.target.value)} />
      <div className="cdr-field-input-row">
        <input className="cdr-field-input" type="text" value={value} placeholder="Value"
          disabled={p.disabled} onChange={(e) => setValue(e.target.value)} />
        <input className="cdr-field-input" type="text" value={unit} placeholder="Unit"
          disabled={p.disabled} onChange={(e) => setUnit(e.target.value)} />
      </div>
    </>
  );
}

function RawJsonInputs(p: FieldEditorInnerProps): ReactElement {
  return (
    <div className="cdr-field-raw">
      <code>
        {JSON.stringify(p.lr.row.payload).slice(0, 160)}
      </code>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// Helpers — labels + confidence
// ────────────────────────────────────────────────────────────────────────────

function _labelFor(row: PendingExtractionRow): string {
  switch (_kind(row)) {
    case 'demographics': return 'Demographics';
    case 'chief_concern': return 'Reason for Visit';
    case 'medication': return 'Medication · Active';
    case 'allergy': return 'Drug Allergy';
    case 'family_history': {
      const v = _payloadValue(row);
      const rel = _str(v.relation);
      return rel ? rel.charAt(0).toUpperCase() + rel.slice(1) : 'Family History';
    }
    case 'code_status': return 'Code Status';
    case 'lab': {
      const code = row.payload as { code?: { coding?: Array<{ display?: string }> } };
      return code.code?.coding?.[0]?.display ?? 'Lab Value';
    }
    case 'other': return 'Custom Entry';
    default: return row.target_resource_type;
  }
}

function _isManualRow(row: PendingExtractionRow): boolean {
  const p = row.payload as { provenance?: unknown };
  return p.provenance === 'manual';
}

function _confidenceFor(row: PendingExtractionRow): { level: 'high' | 'med' | 'low'; label: string } | null {
  const v = _payloadValue(row);
  let raw: unknown = v.confidence ?? (row.payload as Record<string, unknown>).confidence;
  // OCR confidence is sometimes 0..1, sometimes 0..100. Normalize to 0..1.
  let val: number | null = null;
  if (typeof raw === 'number' && Number.isFinite(raw)) {
    val = raw > 1 ? raw / 100 : raw;
  } else if (typeof raw === 'string') {
    if (raw === 'high' || raw === 'medium' || raw === 'low') {
      const map = { high: 0.95, medium: 0.8, low: 0.5 } as const;
      val = map[raw];
    }
  }
  if (val == null) return null;
  if (val >= 0.85) return { level: 'high', label: 'High confidence' };
  if (val >= 0.6) return { level: 'med', label: 'Medium confidence' };
  return { level: 'low', label: 'Low confidence' };
}

function _truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return `${s.slice(0, n - 1)}…`;
}

// ────────────────────────────────────────────────────────────────────────────
// SVG icons (inline so we don't pull a sprite or icon dep)
// ────────────────────────────────────────────────────────────────────────────

function BackIcon(): ReactElement {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      width="14" height="14">
      <path d="M19 12H5m7 7l-7-7 7-7" />
    </svg>
  );
}

function ChevronIcon(): ReactElement {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      width="14" height="14" style={{ opacity: 0.5 }}>
      <path d="M9 18l6-6-6-6" />
    </svg>
  );
}

function CloseIcon(): ReactElement {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      width="16" height="16">
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  );
}

function ChevLeftIcon(): ReactElement {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      width="16" height="16">
      <polyline points="15 18 9 12 15 6" />
    </svg>
  );
}

function ChevRightIcon(): ReactElement {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      width="16" height="16">
      <polyline points="9 18 15 12 9 6" />
    </svg>
  );
}

function ZoomOutIcon(): ReactElement {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      width="16" height="16">
      <circle cx="11" cy="11" r="8" />
      <line x1="21" y1="21" x2="16.65" y2="16.65" />
      <line x1="8" y1="11" x2="14" y2="11" />
    </svg>
  );
}

function ZoomInIcon(): ReactElement {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      width="16" height="16">
      <circle cx="11" cy="11" r="8" />
      <line x1="21" y1="21" x2="16.65" y2="16.65" />
      <line x1="11" y1="8" x2="11" y2="14" />
      <line x1="8" y1="11" x2="14" y2="11" />
    </svg>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// Root + scoped CSS
// ────────────────────────────────────────────────────────────────────────────

const rootStyle: CSSProperties = {
  position: 'fixed',
  inset: 0,
  zIndex: 1100,
  display: 'flex',
  flexDirection: 'column',
};

const REVIEW_CSS = `
.copilot-doc-review {
  --bg: #fafaf7;
  --surface: #ffffff;
  --surface-tint: #f6f5f1;
  --border: #e6e4dd;
  --border-strong: #d4d1c7;
  --ink: #1a1a17;
  --ink-soft: #4a4a44;
  --ink-muted: #8a8a82;
  --accent: #1c3d2e;
  --accent-soft: #e8efe9;
  --accent-deep: #15302a;
  --danger: #8b2615;
  --danger-soft: #faeae6;
  --warn: #8c6914;
  --warn-soft: #fbf3df;
  --success: #1c3d2e;

  --font-display: 'Fraunces', 'Times New Roman', serif;
  --font-body: 'Inter Tight', system-ui, sans-serif;
  --font-mono: 'JetBrains Mono', 'SF Mono', monospace;

  --radius-sm: 4px;
  --radius-md: 6px;
  --radius-lg: 10px;

  --shadow-sm: 0 1px 2px rgba(28, 27, 24, 0.04);
  --shadow-md: 0 1px 3px rgba(28, 27, 24, 0.06), 0 4px 12px rgba(28, 27, 24, 0.04);

  background: var(--bg);
  color: var(--ink);
  font-family: var(--font-body);
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  display: flex;
  flex-direction: column;
}

.copilot-doc-review *, .copilot-doc-review *::before, .copilot-doc-review *::after {
  box-sizing: border-box;
}

.cdr-topbar {
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  padding: 14px 28px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-shrink: 0;
}

.cdr-topbar-left { display: flex; align-items: center; gap: 16px; min-width: 0; }
.cdr-topbar-right { display: flex; align-items: center; gap: 8px; }

.cdr-breadcrumb {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--ink-muted);
  font-size: 13px;
}

.cdr-breadcrumb-link {
  background: transparent;
  border: none;
  font-family: var(--font-body);
  font-size: 13px;
  color: var(--ink-muted);
  cursor: pointer;
  padding: 4px 6px;
  border-radius: var(--radius-sm);
  transition: color 0.15s, background 0.15s;
}
.cdr-breadcrumb-link:hover { color: var(--ink); background: var(--surface-tint); }
.cdr-breadcrumb-back { padding: 4px; display: flex; align-items: center; }
.cdr-breadcrumb-current {
  color: var(--ink);
  font-weight: 500;
  font-size: 13px;
}

.cdr-doc-meta {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--ink-muted);
  padding: 4px 8px;
  background: var(--surface-tint);
  border-radius: var(--radius-sm);
  letter-spacing: 0.02em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 260px;
}

.cdr-status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  font-weight: 500;
  padding: 4px 10px;
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  white-space: nowrap;
}
.cdr-status-pill::before {
  content: '';
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
}
.cdr-status-pill-readonly {
  background: var(--surface-tint);
  color: var(--ink-soft);
  border: 1px solid var(--border);
}
.cdr-status-pill-readonly::before { background: var(--ink-muted); }
.cdr-field-readonly { cursor: default; }
.cdr-field-readonly:hover { box-shadow: none; }

.cdr-btn {
  font-family: var(--font-body);
  font-size: 13px;
  font-weight: 500;
  padding: 8px 16px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--surface);
  color: var(--ink);
  cursor: pointer;
  transition: all 0.15s;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.cdr-btn:hover:not(:disabled) {
  background: var(--surface-tint);
  border-color: var(--ink-muted);
}
.cdr-btn:disabled { cursor: not-allowed; opacity: 0.45; }
.cdr-btn-icon { padding: 8px; }
.cdr-btn-ghost {
  background: transparent;
  border-color: transparent;
  color: var(--ink-soft);
}
.cdr-btn-ghost:hover:not(:disabled) {
  background: var(--surface-tint);
  color: var(--ink);
}

.cdr-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 480px;
  flex: 1;
  min-height: 0;
}

.cdr-doc-viewer {
  background: var(--surface-tint);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-height: 0;
}

.cdr-doc-controls {
  padding: 10px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  color: var(--ink-muted);
  flex-shrink: 0;
}
.cdr-doc-controls-nav { display: flex; align-items: center; gap: 12px; }
.cdr-page-info {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--ink-soft);
  letter-spacing: 0.04em;
  min-width: 90px;
  text-align: center;
}

.cdr-doc-stage {
  flex: 1;
  overflow: auto;
  padding: 32px;
  display: flex;
  justify-content: center;
  align-items: flex-start;
  min-height: 0;
}

.cdr-doc-page-wrap {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  width: 100%;
}

.cdr-doc-page {
  background: white;
  box-shadow: var(--shadow-md);
  border: 1px solid var(--border);
  position: relative;
  max-width: 100%;
}
.cdr-doc-canvas { display: block; }
.cdr-doc-page-image { display: inline-block; }
.cdr-doc-image { display: block; max-width: 100%; height: auto; }
.cdr-doc-page-docx { width: min(680px, 100%); max-height: 70vh; overflow-y: auto; padding: 32px; }

.cdr-bbox-layer {
  position: absolute;
  inset: 0;
  pointer-events: none;
}

.cdr-citation-box {
  position: absolute;
  border: 2px solid var(--accent);
  background: rgba(28, 61, 46, 0.08);
  border-radius: 2px;
  pointer-events: auto;
  cursor: pointer;
  padding: 0;
  transition: all 0.2s;
}
.cdr-citation-box:hover { background: rgba(28, 61, 46, 0.16); }
.cdr-citation-box-active {
  border-color: var(--warn);
  background: rgba(140, 105, 20, 0.12);
  border-width: 2.5px;
  box-shadow: 0 0 0 4px rgba(140, 105, 20, 0.1);
  z-index: 1;
}

/* Citation badge — number-only by default, expands on hover/active to
   show "#N <shortLabel>". Sits above the bbox so it never occludes
   document text inside the citation rectangle itself. */
.cdr-citation-badge {
  position: absolute;
  top: -18px;
  left: -2px;
  display: inline-flex;
  align-items: center;
  background: var(--accent);
  color: white;
  font-family: var(--font-mono);
  font-size: 9px;
  font-weight: 500;
  padding: 2px 5px;
  border-radius: 2px;
  white-space: nowrap;
  letter-spacing: 0.02em;
  max-width: 16px;        /* room for 1-3 digit ordinal */
  overflow: hidden;
  transition: max-width 180ms ease-out, padding 180ms ease-out;
  pointer-events: none;
}
.cdr-citation-badge-number { display: inline-block; }
.cdr-citation-badge-detail {
  opacity: 0;
  transition: opacity 120ms ease-out 60ms;
}
.cdr-citation-box:hover .cdr-citation-badge,
.cdr-citation-box-active .cdr-citation-badge {
  max-width: 360px;
  padding: 2px 7px;
}
.cdr-citation-box:hover .cdr-citation-badge-detail,
.cdr-citation-box-active .cdr-citation-badge-detail {
  opacity: 1;
}
.cdr-citation-box-active .cdr-citation-badge { background: var(--warn); }

/* Feature B — bbox reshape mode. Boxes get a grab cursor + 8 handles
   on the corners and edge midpoints. */
.cdr-bbox-layer-edit .cdr-citation-box { cursor: grab; }
.cdr-bbox-layer-edit .cdr-citation-box:active { cursor: grabbing; }
.cdr-citation-box-edit { border-style: dashed; }
.cdr-bbox-handle {
  position: absolute;
  width: 9px;
  height: 9px;
  background: var(--warn);
  border: 1px solid #fff;
  border-radius: 1px;
  z-index: 2;
}
.cdr-bbox-handle-nw { top: -5px; left: -5px; cursor: nw-resize; }
.cdr-bbox-handle-n  { top: -5px; left: 50%; margin-left: -5px; cursor: n-resize; }
.cdr-bbox-handle-ne { top: -5px; right: -5px; cursor: ne-resize; }
.cdr-bbox-handle-e  { top: 50%; right: -5px; margin-top: -5px; cursor: e-resize; }
.cdr-bbox-handle-se { bottom: -5px; right: -5px; cursor: se-resize; }
.cdr-bbox-handle-s  { bottom: -5px; left: 50%; margin-left: -5px; cursor: s-resize; }
.cdr-bbox-handle-sw { bottom: -5px; left: -5px; cursor: sw-resize; }
.cdr-bbox-handle-w  { top: 50%; left: -5px; margin-top: -5px; cursor: w-resize; }

/* Edit-mode toggle button */
.cdr-btn-edit-toggle {
  font-size: 11px;
  padding: 5px 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-strong);
  background: var(--surface);
  color: var(--ink-soft);
  font-weight: 500;
  margin-left: 6px;
  cursor: pointer;
  transition: all 0.15s;
}
.cdr-btn-edit-toggle:hover { border-color: var(--ink-muted); color: var(--ink); }
.cdr-btn-edit-toggle-active {
  background: var(--warn);
  color: #fff;
  border-color: var(--warn);
}

/* Feature A — manual citation create */
.cdr-btn-create-toggle {
  font-size: 11px;
  padding: 5px 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--accent);
  background: var(--surface);
  color: var(--accent);
  font-weight: 500;
  margin-left: 6px;
  cursor: pointer;
  transition: all 0.15s;
}
.cdr-btn-create-toggle:hover { background: var(--accent-soft); }
.cdr-btn-create-toggle-active {
  background: var(--accent);
  color: #fff;
}

.cdr-create-layer {
  position: absolute;
  inset: 0;
  cursor: crosshair;
  background: rgba(28, 61, 46, 0.04);
  z-index: 3;
}
.cdr-create-preview {
  position: absolute;
  border: 2px dashed var(--accent);
  background: rgba(28, 61, 46, 0.10);
  pointer-events: none;
}

/* Manual entry pill on the rail card */
.cdr-field-manual-pill {
  display: inline-block;
  margin-left: 6px;
  font-size: 9px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--accent);
  color: #fff;
}
.cdr-field-manual {
  border-left: 3px solid var(--accent);
}

/* Modal — manual create form */
.cdr-modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1200;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.cdr-modal {
  background: var(--surface);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-lg);
  width: min(420px, 100%);
  display: flex;
  flex-direction: column;
  box-shadow: 0 16px 48px rgba(15, 23, 42, 0.25);
  overflow: hidden;
}
.cdr-modal-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
}
.cdr-modal-title {
  font-family: var(--font-display);
  font-size: 16px;
  font-weight: 500;
  color: var(--ink);
  flex: 1;
}
.cdr-modal-meta {
  padding: 8px 18px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--ink-muted);
  border-bottom: 1px solid var(--border);
  background: var(--surface-tint);
}
.cdr-modal-body {
  padding: 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.cdr-modal-label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 11px;
  color: var(--ink-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-weight: 500;
}
.cdr-modal-error {
  font-size: 12px;
  color: var(--danger);
  background: var(--danger-soft);
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--danger);
}
.cdr-modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 18px;
  border-top: 1px solid var(--border);
  background: var(--surface-tint);
}

.cdr-docx-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-family: 'Times New Roman', serif;
  color: var(--ink);
  font-size: 13px;
  line-height: 1.55;
}
.cdr-docx-para {
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  border-left: 3px solid transparent;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
  white-space: pre-wrap;
}
.cdr-docx-para:hover { background: var(--surface-tint); }
.cdr-docx-para-active {
  background: var(--warn-soft);
  border-left: 3px solid var(--warn);
}
.cdr-docx-empty, .cdr-docx-empty-line {
  color: var(--ink-muted);
  font-style: italic;
  font-size: 12px;
}

.cdr-rail {
  background: var(--surface);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-height: 0;
}

.cdr-rail-header {
  padding: 16px 24px 12px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}

.cdr-rail-title {
  font-family: var(--font-display);
  font-size: 16px;
  font-weight: 500;
  letter-spacing: -0.01em;
  margin-bottom: 4px;
  color: var(--ink);
}

.cdr-rail-subtitle {
  font-size: 12px;
  color: var(--ink-muted);
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.cdr-dot { width: 3px; height: 3px; border-radius: 50%; background: var(--ink-muted); }

.cdr-field-list {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px 24px;
  min-height: 0;
}

.cdr-field-group { margin-bottom: 24px; }
.cdr-field-group-title {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--ink-muted);
  margin-bottom: 10px;
  padding-left: 4px;
}

.cdr-field {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 12px 14px;
  margin-bottom: 10px;
  transition: all 0.15s;
  cursor: pointer;
  position: relative;
}
.cdr-field:hover {
  border-color: var(--border-strong);
  box-shadow: var(--shadow-sm);
}
.cdr-field-active {
  border-color: var(--warn);
  background: var(--warn-soft);
  box-shadow: 0 0 0 3px rgba(140, 105, 20, 0.08);
}
.cdr-field-approved {
  background: var(--accent-soft);
  border-color: var(--accent);
  opacity: 0.75;
}
.cdr-field-rejected {
  background: var(--danger-soft);
  border-color: var(--danger);
  opacity: 0.6;
}
.cdr-field-error { border-color: var(--danger); }

.cdr-field-row-1 {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  gap: 8px;
}

.cdr-field-label {
  font-size: 11px;
  color: var(--ink-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-weight: 500;
}

.cdr-field-citation {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--ink-muted);
  background: var(--surface-tint);
  padding: 2px 6px;
  border-radius: var(--radius-sm);
}

.cdr-field-input {
  width: 100%;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  font-family: var(--font-body);
  font-size: 13px;
  color: var(--ink);
  background: var(--surface);
  transition: border-color 0.15s, box-shadow 0.15s;
  margin-bottom: 6px;
}
.cdr-field-input:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}
.cdr-field-input:disabled {
  background: var(--surface-tint);
  color: var(--ink-soft);
  cursor: not-allowed;
}
.cdr-field-input-compound { margin-bottom: 6px; }
.cdr-field-input-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  margin-bottom: 6px;
}

.cdr-field-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--border);
}

.cdr-field-confidence {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--ink-muted);
}
.cdr-conf-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}
.cdr-conf-high { background: var(--success); }
.cdr-conf-med { background: var(--warn); }
.cdr-conf-low { background: var(--danger); }
.cdr-conf-muted { color: var(--ink-muted); font-style: italic; }

.cdr-field-buttons { display: flex; gap: 4px; }

.cdr-btn-tiny {
  font-family: var(--font-body);
  font-size: 11px;
  padding: 4px 10px;
  font-weight: 500;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--ink-soft);
  cursor: pointer;
  transition: all 0.12s;
}
.cdr-btn-tiny:hover:not(:disabled) {
  border-color: var(--ink-muted);
  color: var(--ink);
}
.cdr-btn-tiny:disabled { opacity: 0.45; cursor: not-allowed; }
.cdr-btn-tiny-reject:hover:not(:disabled) {
  background: var(--danger-soft);
  border-color: var(--danger);
  color: var(--danger);
}
.cdr-btn-tiny-approve {
  background: var(--accent);
  color: white;
  border-color: var(--accent);
}
.cdr-btn-tiny-approve:hover:not(:disabled) { background: var(--accent-deep); }

.cdr-field-raw {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--ink-muted);
  background: var(--surface-tint);
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  word-break: break-all;
  margin-bottom: 6px;
}

.cdr-action-bar {
  border-top: 1px solid var(--border);
  background: var(--surface);
  padding: 14px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  box-shadow: 0 -4px 20px rgba(28, 27, 24, 0.04);
  flex-shrink: 0;
}

.cdr-progress { display: flex; align-items: center; gap: 12px; min-width: 0; }
.cdr-progress-text {
  font-size: 12px;
  color: var(--ink-soft);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.cdr-progress-text strong { color: var(--ink); font-weight: 600; }
.cdr-progress-bar {
  width: 100px;
  height: 4px;
  background: var(--border);
  border-radius: 2px;
  overflow: hidden;
  flex-shrink: 0;
}
.cdr-progress-fill {
  height: 100%;
  background: var(--accent);
  transition: width 0.3s;
}

.cdr-action-buttons { display: flex; gap: 8px; flex-shrink: 0; }
.cdr-btn-action-reject {
  color: var(--danger);
  border-color: var(--danger);
}
.cdr-btn-action-reject:hover:not(:disabled) { background: var(--danger-soft); }
.cdr-btn-action-approve {
  background: var(--accent);
  color: white;
  border-color: var(--accent);
  padding: 10px 22px;
  font-weight: 600;
}
.cdr-btn-action-approve:hover:not(:disabled) {
  background: var(--accent-deep);
  border-color: var(--accent-deep);
}
.cdr-count {
  background: rgba(255, 255, 255, 0.2);
  padding: 1px 6px;
  border-radius: 3px;
  margin-left: 4px;
  font-family: var(--font-mono);
  font-size: 11px;
}

.cdr-state-msg {
  padding: 24px;
  font-size: 13px;
  color: var(--ink-muted);
  text-align: center;
  font-style: italic;
}
.cdr-state-err { color: var(--danger); font-style: normal; }

.copilot-doc-review ::-webkit-scrollbar { width: 8px; height: 8px; }
.copilot-doc-review ::-webkit-scrollbar-track { background: transparent; }
.copilot-doc-review ::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: 4px; }
.copilot-doc-review ::-webkit-scrollbar-thumb:hover { background: var(--ink-muted); }
`;
