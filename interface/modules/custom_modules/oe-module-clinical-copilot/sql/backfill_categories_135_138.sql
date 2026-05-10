-- =============================================================================
-- backfill_categories_135_138.sql
--
-- One-shot backfill: re-points the Copilot-uploaded documents 135–138 from
-- the bare "Medical Record" category (no `codes` value, invisible to the
-- FHIR DocumentReference search) to the new "Clinical Copilot Upload"
-- category whose `codes` column carries LOINC 34109-9.
--
-- Idempotent / safe to re-run:
--   * The UPDATE is a no-op if categories_to_documents has no rows for
--     documents 135–138 (e.g. on a fresh install where they don't exist).
--   * The UPDATE is a no-op if the "Clinical Copilot Upload" category has
--     not been created yet — the inner SELECT returns NULL and the WHERE
--     clause's `<> id` (or IS NOT NULL) guard short-circuits.
--   * Re-running after the rows are already pointed at the new category
--     leaves them unchanged.
--
-- Run on Railway:
--   railway run -s clinical-copilot-openemr-mysql -- \
--     mariadb -h "$MYSQL_HOST" -P "$MYSQL_PORT" -u "$MYSQL_USER" \
--             -p"$MYSQL_PASS" "$MYSQL_DB" \
--     < interface/modules/custom_modules/oe-module-clinical-copilot/sql/backfill_categories_135_138.sql
-- =============================================================================

UPDATE categories_to_documents
SET    category_id = (
           SELECT id FROM categories WHERE name = 'Clinical Copilot Upload' LIMIT 1
       )
WHERE  document_id IN (135, 136, 137, 138)
  AND  (SELECT id FROM categories WHERE name = 'Clinical Copilot Upload' LIMIT 1) IS NOT NULL
  AND  category_id <> (SELECT id FROM categories WHERE name = 'Clinical Copilot Upload' LIMIT 1);
