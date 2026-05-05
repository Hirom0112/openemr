-- Clinical Co-Pilot PHI audit log — Postgres schema.
--
-- Append-only by convention; the writer issues only INSERTs and the
-- destruction-record API records purges in a separate immutable table.
-- Partitioning by month-range on ``ts`` makes the §9.7 6-year retention
-- policy trivially enforceable via DROP PARTITION.

CREATE TABLE IF NOT EXISTS copilot_audit_events (
    id           BIGSERIAL,
    ts           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type   TEXT NOT NULL,           -- request | tool_call | scope_violation | auth_failure | destruction
    request_id   TEXT,
    session_id   TEXT,
    provider_id  TEXT,
    patient_id   TEXT,                    -- nullable; only for patient-scoped events
    tool_name    TEXT,                    -- nullable; only for tool_call events
    method       TEXT,
    path         TEXT,
    status_code  INTEGER,
    duration_ms  INTEGER,
    outcome      TEXT,                    -- success | failure | denied
    detail_json  JSONB,                   -- bounded structured detail (no PHI free text!)
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

-- Initial yearly partition; the operator runs ``pg_partman`` or manual
-- ALTER for ongoing rolling. For the local demo, create one partition
-- that covers the current year.
DO $$
DECLARE
    start_ts DATE := date_trunc('year', CURRENT_DATE)::date;
    end_ts   DATE := (date_trunc('year', CURRENT_DATE) + INTERVAL '1 year')::date;
    pname    TEXT := 'copilot_audit_events_' || to_char(start_ts, 'YYYY');
BEGIN
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF copilot_audit_events FOR VALUES FROM (%L) TO (%L)',
        pname, start_ts, end_ts
    );
END $$;

CREATE INDEX IF NOT EXISTS idx_audit_session  ON copilot_audit_events (session_id, ts);
CREATE INDEX IF NOT EXISTS idx_audit_patient  ON copilot_audit_events (patient_id, ts);
CREATE INDEX IF NOT EXISTS idx_audit_provider ON copilot_audit_events (provider_id, ts);

-- Destruction records — separate table so destruction events themselves
-- are immutable.  No DELETE / UPDATE grant should ever be issued.
CREATE TABLE IF NOT EXISTS copilot_audit_destructions (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    requested_by    TEXT NOT NULL,         -- provider_id from JWT
    request_id      TEXT,
    target_session  TEXT,
    target_patient  TEXT,
    target_window   TSTZRANGE,
    rows_affected   INTEGER,
    reason          TEXT NOT NULL          -- compliance reason; required
);

-- ── Document extraction records (W2 §4.3 / §4.4) ────────────────────────────
--
-- One row per (DocumentReference, content hash) extraction attempt. The
-- (document_reference_id, content_sha256) UNIQUE constraint backs the
-- stub-row INSERT…ON CONFLICT DO NOTHING dance that turns concurrent
-- workers into a single winner. JSONB payload holds the full
-- ExtractionResult including per-field bbox metadata; bboxes don't fit
-- FHIR cleanly, so they live here rather than in OpenEMR.
CREATE TABLE IF NOT EXISTS copilot_doc_extractions (
    extraction_id          BIGSERIAL PRIMARY KEY,
    document_reference_id  TEXT NOT NULL,
    content_sha256         TEXT NOT NULL,
    patient_id             TEXT NOT NULL,
    status                 TEXT NOT NULL CHECK (status IN ('processing','complete','failed','permanently_failed')),
    extraction_kind        TEXT,                       -- 'lab_report' | 'intake_form' | 'unknown' | NULL while processing
    extraction_payload     JSONB,                      -- full ExtractionResult JSON; NULL while processing
    classifier_confidence  DOUBLE PRECISION,
    ocr_confidence_min     DOUBLE PRECISION,
    ocr_confidence_max     DOUBLE PRECISION,
    processing_started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at           TIMESTAMPTZ,
    retry_count            INTEGER NOT NULL DEFAULT 0,
    retry_after            TIMESTAMPTZ,
    last_error             TEXT,
    UNIQUE (document_reference_id, content_sha256)
);
CREATE INDEX IF NOT EXISTS copilot_doc_extractions_status_idx ON copilot_doc_extractions (status, processing_started_at);
CREATE INDEX IF NOT EXISTS copilot_doc_extractions_doc_ref_idx ON copilot_doc_extractions (document_reference_id);

-- ── Hybrid-RAG guideline corpus (W2 §6 / §17.4) ─────────────────────────────
--
-- One row per chunk of indexed clinical guideline text. Sparse retrieval uses
-- the GENERATED tsvector + GIN index; dense retrieval uses pgvector cosine
-- distance via ivfflat. Both indices are built from the same row so the
-- merge step in rag.retrieve.search can dedupe on chunk_id.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS copilot_guideline_chunks (
    chunk_id              TEXT PRIMARY KEY,
    source_id             TEXT NOT NULL,
    document_title        TEXT NOT NULL,
    section               TEXT,
    page_number           INTEGER,
    indexed_version_date  DATE NOT NULL,
    content               TEXT NOT NULL,
    content_tsv           TSVECTOR
                            GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    embedding             vector(1024),
    indexed_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS copilot_guideline_chunks_tsv_idx
    ON copilot_guideline_chunks USING GIN (content_tsv);
CREATE INDEX IF NOT EXISTS copilot_guideline_chunks_embedding_idx
    ON copilot_guideline_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
CREATE INDEX IF NOT EXISTS copilot_guideline_chunks_source_idx
    ON copilot_guideline_chunks (source_id);
