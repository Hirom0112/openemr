# Conditions → Problem List bridge — Phase 6.3 verification (PARTIAL)

Verified 2026-05-10 against local Docker (`development-easy-openemr-1`, `development-easy-agent-api-1`, `development-easy-mysql-1`).

## What works (verified live)

End-to-end FHIR write/read chain for Whitaker, pid=27, FHIR uuid `0bd04225-48ef-11f1-8ae6-e6dc9d318bb3`:

1. agent-api `observations.writer.write_condition` POSTs FHIR R4 Condition body to
   `POST /interface/modules/custom_modules/oe-module-clinical-copilot/public/condition.php`
   (custom JWT shim, `COPILOT_JWT_SECRET`, issuer `openemr-copilot`).
2. `ConditionController::handle()` UPSERTs into module-private `copilot_conditions`
   table (NOT core `lists`). Test row observed:
   `id=copilot-999-phase63-test, pid=27, icd10=I10, condition_text="...essential hypertension", clinical_status=active, onset_date=2024-03-15`.
3. `GET /apis/default/fhir/Condition?patient={uuid}&category=problem-list-item`
   returns the row. The aggregator `src/Services/FHIR/FhirConditionService.php:62`
   has `addMappedService(new FhirConditionCopilotService())` registered (the
   second documented exception to "no core edits", per `agent-api/CLAUDE.md`).
4. Bidirectional confirmed: a row inserted directly into core `lists`
   (type=medical_problem, with uuid populated) is also returned by the same
   FHIR call alongside the copilot row — both surfaces project as
   category=problem-list-item without dedup collisions because copilot ids
   never overlap UUIDs.

## What does NOT work — chart UI gap

The stock OpenEMR Issues / Problem List chart sidebar widget renders by querying
`lists` directly:

- `interface/patient_file/summary/stats.php:48` — `SELECT * FROM lists WHERE pid=? AND type=? ...`
- `interface/patient_file/summary/stats_full.php:257` — same shape

It does NOT consume the FHIR aggregator, so `copilot_conditions` rows do not
appear in the chart Problem List sidebar. They appear ONLY when a consumer hits
`/apis/default/fhir/Condition` (e.g. external EHR, our agent-api itself, the
Copilot panel if it ever queries Conditions).

## Gap scope (do NOT fix in this phase)

To dual-surface in the chart UI without touching core, the allowed move is a
custom sidebar fragment under
`interface/modules/custom_modules/oe-module-clinical-copilot/` that reads
`copilot_conditions` directly (Path B2 sibling to the Copilot Labs card from
todo.md §6.2). Cost: ~1 small PHP fragment + ACL gate + sidebar registration
hook. Not in scope for §6.3 — §6.3 was verification only.

## Verdict

PARTIAL. FHIR write-through and FHIR read-through both work end-to-end with
correct code/onset/clinical-status display. Chart-UI sidebar surfacing is
intentionally out of scope for the FHIR bridge; closing it requires a custom
module sidebar card, not a fix to the Conditions path itself.
