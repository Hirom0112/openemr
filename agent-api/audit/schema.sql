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

-- Phase-2 follow-up: per-LabValue FHIR Observation provenance ids.
-- ``/document/ingest`` writes one Observation per extracted lab value
-- (deterministic id "copilot-{document_id}-{loinc_code}") via the custom
-- oe-module-clinical-copilot endpoint and records the resulting ids here.
ALTER TABLE copilot_doc_extractions
    ADD COLUMN IF NOT EXISTS observation_ids JSONB;

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

-- ── Phase 9 Slice 9.1 — Multimodal pending-write + quarantine schema ─────────
--
-- Multimodal ingestion (DOCX / HL7 v2 / TIFF / XLSX) stages every clinical
-- write before it lands in OpenEMR. ``copilot_pending_extractions`` is the
-- per-record staging table; ``copilot_quarantined_documents`` is the holding
-- pen for documents whose patient identity could not be resolved with
-- sufficient confidence (see todo.md Phase 9 Slices 9.1–9.3).
--
-- Idempotency: every CREATE here uses IF NOT EXISTS to match the rest of
-- this file, so re-bootstrapping the schema on an existing volume is a
-- no-op. The revival-blocking trigger is created with CREATE OR REPLACE
-- FUNCTION + DROP TRIGGER IF EXISTS to stay idempotent.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Pending extractions: one row per (DocumentReference, target FHIR resource)
-- staged for clinician approval before the FHIR write fires.
--
-- target_resource_type is one of four: Observation (lab values), Task
-- (Care_Gaps from XLSX), AllergyIntolerance (XLSX Patient sheet allergies),
-- or IntakeFormField (PDF/DOCX intake-form fields without FHIR writers —
-- allergies/meds/demographics/family-hx/chief-concern/code-status. These
-- rows track per-field review state but never trigger a FHIR write on
-- approve; ``state='approved'`` is the terminal state for them).
-- target_resource_id mirrors the deterministic id produced by
-- ``observations.writer.deterministic_observation_id`` and the equivalent
-- minters for Task / AllergyIntolerance — the regex is the same one the
-- PHP ObservationController enforces at oe-module-clinical-copilot/
-- src/ObservationController.php:51.
--
-- State machine:
--   pending → approved → written      (happy path)
--   pending → rejected                (clinician declined)
--   approved → failed                 (writer error)
--   failed   → approved               (explicit retry; only legal terminal
--                                      → non-terminal transition)
-- Terminal states (rejected, written, failed) are otherwise immutable —
-- enforced by the BEFORE UPDATE trigger below. Re-staging a rejected/written
-- record requires a new row with a new target_resource_id (partial unique
-- index on (document_reference_id, target_resource_id) WHERE state='pending'
-- allows that without dropping the global uniqueness invariant).
CREATE TABLE IF NOT EXISTS copilot_pending_extractions (
    id                     BIGSERIAL PRIMARY KEY,
    document_reference_id  TEXT NOT NULL,
    file_batch_id          UUID NOT NULL,
    patient_id             TEXT NOT NULL,
    target_resource_type   TEXT NOT NULL
        CHECK (target_resource_type IN ('Observation', 'Task', 'AllergyIntolerance', 'IntakeFormField')),
    target_resource_id     TEXT NOT NULL
        CHECK (target_resource_id ~ '^copilot-\d+-[\w.\-]+$'),
    state                  TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'approved', 'rejected', 'written', 'failed')),
    payload                JSONB NOT NULL,
    write_error            TEXT,
    retry_count            INTEGER NOT NULL DEFAULT 0,
    staged_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at             TIMESTAMPTZ,
    decided_by             TEXT,
    written_at             TIMESTAMPTZ
);

-- Partial unique on (document_reference_id, target_resource_id) restricted
-- to pending rows: terminal-state rows are immutable and may co-exist with
-- a freshly-staged retry row that targets a NEW deterministic id. We do
-- NOT add a global UNIQUE constraint on the same pair — that would block
-- legitimate post-rejection re-staging via a different resource id.
CREATE UNIQUE INDEX IF NOT EXISTS copilot_pending_extractions_pending_unique_idx
    ON copilot_pending_extractions (document_reference_id, target_resource_id)
    WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS copilot_pending_extractions_patient_state_idx
    ON copilot_pending_extractions (patient_id, state);
CREATE INDEX IF NOT EXISTS copilot_pending_extractions_file_batch_idx
    ON copilot_pending_extractions (file_batch_id);
CREATE INDEX IF NOT EXISTS copilot_pending_extractions_state_staged_idx
    ON copilot_pending_extractions (state, staged_at);

-- Revival-blocking trigger: terminal states (rejected, written, failed)
-- are immutable except for the explicit retry transition failed → approved.
-- Any other state mutation away from a terminal state raises an exception
-- — the staging API must INSERT a new row instead.
CREATE OR REPLACE FUNCTION copilot_pending_extractions_block_revival()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.state IN ('rejected', 'written', 'failed') AND NEW.state <> OLD.state THEN
        IF OLD.state = 'failed' AND NEW.state = 'approved' THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION
            'copilot_pending_extractions: terminal state % is immutable (id=%, attempted new state=%)',
            OLD.state, OLD.id, NEW.state;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS copilot_pending_extractions_block_revival_trg
    ON copilot_pending_extractions;
CREATE TRIGGER copilot_pending_extractions_block_revival_trg
    BEFORE UPDATE ON copilot_pending_extractions
    FOR EACH ROW
    EXECUTE FUNCTION copilot_pending_extractions_block_revival();

-- Quarantined documents: pre-created here so schema migrations stay atomic
-- even though the resolver that populates this table lands in Slice 9.2.
-- One row per upload whose patient identity could not be resolved with
-- sufficient confidence. Operators (panel-scoped) match, claim, or reject.
CREATE TABLE IF NOT EXISTS copilot_quarantined_documents (
    id                     BIGSERIAL PRIMARY KEY,
    quarantine_id          UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    document_reference_id  TEXT NOT NULL,
    file_batch_id          UUID,
    panel_id               TEXT,
    parsed_identity        JSONB NOT NULL,
    candidate_matches      JSONB,
    reason_code            TEXT NOT NULL,
    state                  TEXT NOT NULL DEFAULT 'unclaimed'
        CHECK (state IN ('unclaimed', 'claimed', 'matched', 'rejected', 'expired')),
    claimed_by             TEXT,
    claimed_at             TIMESTAMPTZ,
    claim_expires_at       TIMESTAMPTZ,
    resolved_patient_id    TEXT,
    resolved_at            TIMESTAMPTZ,
    resolved_by            TEXT,
    rejected_reason        TEXT,
    quarantined_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at             TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '36 hours'
);
CREATE INDEX IF NOT EXISTS copilot_quarantined_documents_state_idx
    ON copilot_quarantined_documents (state, quarantined_at);
CREATE INDEX IF NOT EXISTS copilot_quarantined_documents_panel_idx
    ON copilot_quarantined_documents (panel_id, state);
CREATE INDEX IF NOT EXISTS copilot_quarantined_documents_doc_ref_idx
    ON copilot_quarantined_documents (document_reference_id);
