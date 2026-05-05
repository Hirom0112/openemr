/**
 * Citation / bbox / soft-warn types for the W2 document-extraction surface.
 *
 * Mirrors the Python schemas in `agent-api/extractors/schemas.py` so the React
 * renderer is a structural twin of what the backend emits — keep these in
 * lockstep when the Python side evolves.
 */

export type CitationSourceType = 'document' | 'observation' | 'guideline';

export interface Citation {
  source_type: CitationSourceType;
  source_id: string;
  /** Page number (documents) or section anchor (guidelines). May be null for observations. */
  page_or_section: string | null;
  /** Bbox id like "p2-b005" for documents, chunk id for guidelines, FHIR ref for observations. */
  field_or_chunk_id: string;
  quote_or_value: string;
  /**
   * Optional bbox in PDF user-space coordinates ([x, y, w, h], 1pt = 1/72in,
   * top-left origin). Sent by the backend on every document citation in the
   * ingest response so the viewer can highlight without a layout-table lookup.
   * Falls back to {@link BboxLayoutBlock} resolution when absent.
   */
  bbox?: [number, number, number, number] | null;
  /**
   * Optional 1-indexed page number. Sent by the backend alongside `bbox`.
   * Falls back to a numeric parse of `page_or_section` when absent.
   */
  page?: number | null;
  /**
   * Optional polygon (Wave 2B) — list of [x, y] pairs in the SAME coord
   * frame as `bbox`. When present and non-degenerate (≥3 distinct points)
   * the viewer renders the polygon shape rather than the bbox rectangle.
   * Null/undefined means the OCR engine had no polygon source (tesseract,
   * PDF text-layer) — fall back to `bbox`.
   */
  polygon?: Array<[number, number]> | null;
  /**
   * Frontend-only human-readable description (e.g. "Sodium 138 mEq/L") used
   * by {@link CitationChip} to disambiguate sibling chips. Derived in
   * `ChatSurface` from the extraction shape; never sent on the wire.
   */
  label?: string | null;
}

/** One OCR layout block — ties bbox_id back to a rectangle on a specific PDF page. */
export interface BboxLayoutBlock {
  bbox_id: string;
  page: number;
  /** [x, y, w, h] in PDF points (1pt = 1/72in). Origin is top-left of the page. */
  bbox: [number, number, number, number];
  text: string;
  ocr_confidence: number;
  /**
   * Block granularity, set by the backend extraction layer. "word" = a single
   * word-level OCR box; "line" = an aggregated line-level box. The frontend
   * uses this purely for an affordance hint (dashed vs solid bbox border).
   * Optional for backwards-compatibility with cached responses produced
   * before this field was introduced.
   */
  granularity?: 'word' | 'line';
  /**
   * Optional polygon (Wave 2B) — list of [x, y] pairs in the same coord
   * frame as `bbox`. Mirrors `LayoutBlock.polygon` on the Python side.
   * Optional for backwards-compatibility: tesseract / PDF text-layer
   * blocks have no polygon source.
   */
  polygon?: Array<[number, number]> | null;
}

/** A non-fatal extraction warning the UI surfaces as a yellow banner. */
export interface SoftWarn {
  code: string;
  message: string;
  fields: string[];
}
