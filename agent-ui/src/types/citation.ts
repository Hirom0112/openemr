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
}

/** One OCR layout block — ties bbox_id back to a rectangle on a specific PDF page. */
export interface BboxLayoutBlock {
  bbox_id: string;
  page: number;
  /** [x, y, w, h] in PDF points (1pt = 1/72in). Origin is top-left of the page. */
  bbox: [number, number, number, number];
  text: string;
  ocr_confidence: number;
}

/** A non-fatal extraction warning the UI surfaces as a yellow banner. */
export interface SoftWarn {
  code: string;
  message: string;
  fields: string[];
}
