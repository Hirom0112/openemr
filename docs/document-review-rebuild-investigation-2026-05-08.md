# DocumentReviewPanel rebuild — Phase 0 investigation

**Date:** 2026-05-08
**Mock source of truth:** `docs/document-review-mock.html`
**Component to replace:** `agent-ui/src/components/DocumentReviewPanel.tsx` (816 lines)

---

## a) Current data shape

### Props (DocumentReviewPanelProps)

```ts
{
  baseUrl: string;
  patientId: string;
  documentReferenceId: string;
  fileBatchId: string;
  pendingRowIds: number[];
  onClose: () => void;
  onCompleted: (docRefId: string, ragResult: PostApprovalContext | null) => void;
}
```

`reviewTarget` in `App.tsx:37-41` carries `documentReferenceId`, `fileBatchId`,
`rowIds[]`. The panel pulls each row by id via `getPendingOne(baseUrl, id)` —
no batched-fetch endpoint, but row count is bounded by `ROW_LIMIT = 50`.

### PendingExtractionRow (api.ts:687)

```ts
{
  id: number;
  document_reference_id: string;
  file_batch_id: string;
  patient_id: string;
  target_resource_type: 'Observation' | 'Task' | 'AllergyIntolerance' | 'IntakeFormField';
  target_resource_id: string;          // e.g. "copilot-{docId}-intake-allergy-2"
  state: 'pending' | 'approved' | 'rejected' | 'written' | 'failed';
  payload: Record<string, unknown>;    // FHIR-shaped (Observation) OR { kind, value: {...} } (IntakeFormField)
  write_error?: string | null;
  ...
}
```

### Citation shape (types/citation.ts)

Citations live INSIDE `payload` — for IntakeFormField under `payload.value.citations`,
for Observation under `payload.derivedFrom` plus per-field citations. The existing
`_collectCitations()` in DocumentReviewPanel.tsx:92-135 already harvests them; the
shape is:

```ts
{
  source_type: 'document' | 'observation' | 'guideline';
  source_id: string;
  page_or_section: string | null;
  field_or_chunk_id: string;            // bbox_id, e.g. "p2-b005"
  quote_or_value: string;
  bbox?: [x, y, w, h] | null;           // PDF user-space points (1/72in), top-left origin
  page?: number | null;                 // 1-indexed
  polygon?: Array<[x, y]> | null;       // Wave 2B alternative shape
}
```

**Bbox data is already in scope.** The backend's `_normalize_citation_bbox`
backfill (`agent-api/main.py:1463`) ensures every citation in the staged payload
carries `bbox` + `page` even on cached responses. No backend change required for
Phase 3 — the inventory's "not currently surfaced" comment at the old line 371
is wrong.

---

## b) Existing FieldEditors (33 KB, 7 editors)

| target_resource_type | Editor                | Mock category match |
|----------------------|-----------------------|---------------------|
| Observation          | LabValueEditor        | (Lab Values; not in this mock) |
| IntakeFormField allergy        | AllergyEditor       | Allergies |
| IntakeFormField medication     | MedicationEditor    | Current Medications |
| IntakeFormField demographics   | DemographicsEditor  | Demographics |
| IntakeFormField family_history | FamilyHistoryEditor | Family History |
| IntakeFormField chief_concern  | ChiefConcernEditor  | Chief Concern |
| IntakeFormField code_status    | CodeStatusEditor    | (no mock entry) |

Editor protocol (`FieldEditorProps`):

```ts
{
  row: PendingExtractionRow;
  busy: boolean;
  onValueChange: (rowId, override | null) => void;  // emits FULL merged payload
  onApprove: (rowId) => void;
  onReject: (rowId, reason) => void;
  onCitationClick?: (citationFieldId, page) => void;
  status: 'idle' | 'busy' | 'done' | 'error';
  statusMessage?: string | null;
}
```

`resolveEditor(row)` (FieldEditors.tsx:808) picks the right editor by
`target_resource_type` and (for IntakeFormField) `payload.kind` /
`target_resource_id`-suffix parse.

**Gaps vs. the mock:**

1. The existing editors render heavy multi-row forms with section labels;
   the mock wants a tight compact card (label + citation chip → input(s) →
   footer with confidence dot + tiny Approve/Reject).
2. Editors emit "Source →" buttons; the mock has no Source button — citation
   linking is implicit via card-click highlighting the bbox.
3. No "confidence dot" affordance in the existing editors. The payload does
   not always carry a confidence value — for v1 we'll derive it from
   ocr_confidence on the linked layout block when present, otherwise hide
   the dot rather than fake it.
4. Reject reason is collected by `window.prompt` in the existing code —
   acceptable for v1, the mock doesn't change that.

**Decision:** instead of styling the existing 33 KB FieldEditors module to
match the mock, the new DocumentReviewPanel will inline lightweight editors
keyed by `payload.kind` directly. The old FieldEditors.tsx stays in tree
(ApprovalModal still imports nothing from it; only DocumentReviewPanel did)
but the new panel won't use it. The old editors are still importable if
needed; we're not deleting them in this phase.

---

## c) Citation bbox data flow

- **Coord system:** PDF user-space points (1pt = 1/72in), top-left origin —
  same frame as `BboxOverlay` already consumes. `BboxLayoutBlock.bbox` and
  `Citation.bbox` use the same shape.
- **Per-page:** yes — `Citation.page` is 1-indexed; the bbox is positioned
  inside that page's viewport. PDF page dimensions are read from
  `page.getViewport({ scale: 1 })` inside `DocumentViewer.tsx:171`.
- **Reliability:**
  - **PDF:** reliable when the extractor produced bbox + page. Cached
    payloads were back-filled by the normalizer.
  - **PNG/JPEG:** the image branch in DocumentViewer.tsx:345-386 already
    uses BboxOverlay against image-pixel-space bboxes; same overlay math.
  - **DOCX:** no bbox — the locator is a `field_or_chunk_id` like
    `para=13|run=2`. The current code falls back to a paragraph viewer
    with a highlighted-paragraph hint and that's the right v1 path.
- **Active state:** `BboxOverlay` itself only draws ONE rectangle (the
  active citation). For the mock's "all citations visible, active one
  highlighted" behavior we need to either render N overlays manually
  (one per citation) or wrap BboxOverlay calls. We'll do the former —
  draw all citations on the active page with a non-active style, plus
  the active one in warn color.

---

## d) Mock → existing-code mapping

| Mock feature | Existing code | New work |
|---|---|---|
| Top bar with breadcrumb + doc meta + status pill + open-in-tab icon | Old `headerStyle` block (DocumentReviewPanel.tsx:589) had only a title + doc id chip. | Rewrite header. |
| Two-column 1fr / 480px layout | Old `bodyStyle` was 1.2fr / 1fr. | Adjust gridTemplate. |
| PDF prev/next/zoom controls | `DocumentViewer` only does keyboard-arrow page cycling driven by `activeCitationIndex`. No explicit page nav UI. | Add a doc-controls strip with prev/next/zoom buttons. |
| Per-page bbox overlays with `#id label` badge | `BboxOverlay` draws ONE active bbox; no label badge. | Render N overlays + add label spans. |
| Active-state linking field↔bbox | `onCitationClick` callback exists; activeCitationIndex drives viewer. Reverse direction (bbox click → field highlight) does NOT exist. | Add activeRowId state; bbox onClick → setActiveRowId; field onClick → setActiveCitationIndex. |
| Right rail grouped sections (Demographics / Chief Concern / Medications / Allergies / Family History) | Rows render in arrival order, no grouping. | Group by `payload.kind` (or target_resource_type for non-Intake). |
| Compact field card with label + citation chip + compound inputs + confidence dot + tiny buttons | None — existing editors are larger and styled differently. | Inline new editors per kind. |
| Bottom action bar with progress bar + Reject all + Approve all | Old `footerStyle` had Cancel + "Save & Approve". | Replace with mock's pattern. |
| Confidence dots (high/med/low) | No code path. Confidence not surfaced in payload. | Derive from `ocr_confidence` of the linked layout block when present; otherwise hide. |
| Fonts: Fraunces (display), Inter Tight (body), JetBrains Mono (mono) | Not loaded; existing UI uses system stack. | Inject `<link>` tags for these fonts on panel mount; remove on unmount. |
| Color palette (--bg #fafaf7, --accent #1c3d2e, --warn #8c6914, etc.) | Existing `tokens.ts` uses BRAND #1E3A8A (blue) and SURFACE.bg #FFFFFF — different palette. | Inline CSS variables on the panel root; no global token mutation. |

**Scope note:** the mock fonts + palette are *only* applied to the new
DocumentReviewPanel root element via CSS variables on a scoped class. The
rest of the agent-ui (Census, Brief, Meds) keeps the existing blue brand
tokens. This avoids cross-component bleed and keeps the diff localized.

---

## e) Build plan summary

Single rewrite of `DocumentReviewPanel.tsx`:

1. **Phase 1 (visual shell):** new layout, fonts, palette, mock data fallback.
   File-replace, ~700 lines. Keep all existing prop/callback contracts.
2. **Phase 2 (real data):** group real rows by kind, render compact inline
   editors, wire `onValueChange` / `onApprove` / `onReject` / staging API.
3. **Phase 3 (citations):** render N bbox overlays per page (PDF + image),
   bidirectional click linking, prev/next-page nav, active-state highlight.
4. **Phase 4 (bulk + polish):** Approve all (uses existing `approveBatch`),
   Reject all (loop `rejectOne` after confirm), progress bar, animations,
   Esc-to-close.

No backend change. ApprovalModal, DocumentsTab, ChatSurface, FieldEditors
are not touched. App.tsx routing is not touched.

**Verification per phase:** local ingest of a fresh PDF or PNG fixture,
walk the upload → DocumentReviewPanel render → click → edit → approve
chain end-to-end with HTTP-code + DB-row-state proof.
