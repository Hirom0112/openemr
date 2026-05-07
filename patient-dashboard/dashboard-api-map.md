# Dashboard API Map

Phase 1.4 artifact. For each in-scope dashboard card this file names the FHIR
endpoint(s), maps inventory fields to FHIR JSON paths, and cites the route
registration line + controller that backs the endpoint. Open assumptions are
flagged for the 1.5 curl-verification pass.

Companion to: `dashboard-inventory.md` (field-level spec), `auth-notes.md`
(OAuth2/SMART notes — pending), `architecture-finding.md` (rendering model).

## Conventions

- **Base URL:** `/apis/default/fhir`
- **Auth:** Bearer token from OAuth2 / SMART-on-FHIR flow. The current OpenEMR
  dashboard does NOT consume FHIR (verified in `dashboard-inventory.md` →
  Architecture finding); the port is the first FHIR consumer of these
  endpoints from the patient-summary use case. Auth scope and grant type
  TBD in `auth-notes.md`.
- **Patient identity:** the dashboard receives a numeric `pid` from the URL
  query string (legacy session). FHIR uses the patient UUID. The port will
  resolve `pid -> uuid` once at session start (TODO: confirm in 1.5 — likely
  via `GET /apis/default/api/patient/{pid}` or by pre-resolving from
  `patient_data.uuid` server-side at SMART-launch time). Every FHIR request
  in this map assumes a UUID is in hand.
- **Route file:** `apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php`
  (cited as `_rest_routes_fhir_r4_us_core_3_1_0.inc.php:N`).
- **Controllers:** `src/RestControllers/FHIR/Fhir{Resource}RestController.php`.
- **Services:** the FHIR-shape mapping (OpenEMR row -> FHIR JSON) lives in
  `src/Services/FHIR/Fhir{Resource}Service.php` (or `.../Observation/...` for
  vitals). Service files are the authoritative source for field paths.

## Resource -> endpoint summary

| Card             | FHIR resource         | Endpoint pattern                                                  |
|------------------|-----------------------|-------------------------------------------------------------------|
| Patient header   | Patient               | `GET /Patient/{uuid}`                                             |
| Allergies        | AllergyIntolerance    | `GET /AllergyIntolerance?patient={uuid}`                          |
| Medical Problems | Condition             | `GET /Condition?patient={uuid}&category=problem-list-item`        |
| Medications      | MedicationRequest     | `GET /MedicationRequest?patient={uuid}` (filter client-side, see section) |
| Prescriptions    | MedicationRequest     | `GET /MedicationRequest?patient={uuid}&intent=order&status=active` |
| Care Team        | CareTeam              | `GET /CareTeam?patient={uuid}&status=active`                      |
| Vitals           | Observation           | `GET /Observation?patient={uuid}&category=vital-signs`            |

## Patient header

**Endpoint:** `GET /apis/default/fhir/Patient/{uuid}`

**Route:** `_rest_routes_fhir_r4_us_core_3_1_0.inc.php:610` (`GET /fhir/Patient/:uuid`).
List variant at line 578 (`GET /fhir/Patient`).

**Controller:** `OpenEMR\RestControllers\FHIR\FhirPatientRestController`
(imported at route file line 46).

**Service:** `src/Services/FHIR/FhirPatientService.php` (search params at lines
128–161).

| Inventory field     | FHIR path                                 | Notes |
|---------------------|-------------------------------------------|-------|
| Patient name        | `name[0].given[0]` + `name[0].family`     | Inventory: `patient_data.fname` + `lname` |
| MRN / pubpid        | `identifier[?(@.system contains 'pubpid')].value` | Search param maps `identifier` token -> `ss`, `pubpid` (service line 128). Confirm in 1.5 which `system` URI OpenEMR emits for pubpid. |
| DOB                 | `birthDate`                               | FHIR R4 returns `YYYY-MM-DD`; matches inventory format. |
| Age                 | computed client-side                       | Not a FHIR field. Compute from `birthDate` against `ClockInterface`. |
| Patient avatar      | ⚠ NOT FOUND on FHIR Patient (US Core)     | OpenEMR's `pic_array($pid, ...)` reads the documents store; FHIR Patient does not surface a `photo` element here. TODO 1.5: confirm whether `Patient.photo[0].data` or `Patient.photo[0].url` is populated; if not, fall back to a non-FHIR `/api/patient/{pid}/document?category=patient_photo` call. |
| Close button        | n/a — UI control                          | Clears the dashboard's local `pid` state. |
| Encounter picker    | `GET /Encounter?patient={uuid}` (out of scope for 1.4 — header reads count only) | Route at `_rest_routes_fhir_r4_us_core_3_1_0.inc.php:299`. The "(N)" badge is a count of returned bundle entries. |
| Open Encounter      | `GET /Encounter?patient={uuid}&status=in-progress` | TODO 1.5: confirm `status=in-progress` maps to OpenEMR's "open" encounter concept. |
| New encounter       | n/a — POST flow                           | Out of scope for the read-only port slice. |

## Allergies

**Endpoint:** `GET /apis/default/fhir/AllergyIntolerance?patient={uuid}`

**Route:** `_rest_routes_fhir_r4_us_core_3_1_0.inc.php:73` (list),
`_rest_routes_fhir_r4_us_core_3_1_0.inc.php:85` (read).

**Controller:** `OpenEMR\RestControllers\FHIR\FhirAllergyIntoleranceRestController`
(import at line 26).

**Service:** `src/Services/FHIR/FhirAllergyIntoleranceService.php`.

| Inventory field | FHIR path                                                  | Notes |
|-----------------|------------------------------------------------------------|-------|
| Allergen name   | `code.text` (fall back to `code.coding[0].display`)        | Source: `lists.title`. RxNorm/SNOMED codings populated when available. |
| Reaction        | `reaction[0].manifestation[0].text` (or `.coding[0].display`) | Source: `lists.reaction`. Inventory renders this as a tooltip. |
| Severity label  | `reaction[0].severity` (`mild` / `moderate` / `severe`)    | OpenEMR's `severity_al` enum is mapped — but the more useful field for the highlight rule is `criticality` (see below). |
| Severity flag (highlight) | `criticality` (`low` / `high` / `unable-to-assess`) | Service maps `severity_al` -> `criticality` at lines 138–151. `severe` / `life_threatening_severity` / `fatal` -> `criticality=high`. The dashboard's `bg-warning` rule should key off `criticality=high` for the FHIR port. |
| Active filter (carry forward)   | `clinicalStatus.coding[0].code in [active, inactive]` excluding `resolved` | Inventory's `filterActiveIssues()` excludes outcome=resolved. FHIR equivalent: filter `clinicalStatus != resolved` client-side. |

States ("No Known Allergies" vs "Nothing Recorded"): not directly modelled in
FHIR. The empty-bundle case must be disambiguated client-side. ⚠ The
"touched" / "untouched" distinction (inventory states) is an OpenEMR-internal
flag and has **no FHIR equivalent**. TODO 1.5: decide whether to render a
single empty state ("No allergies on file") or to call a separate non-FHIR
endpoint to recover `listTouched`.

## Medical Problems

**Endpoint:** `GET /apis/default/fhir/Condition?patient={uuid}&category=problem-list-item`

**Route:** `_rest_routes_fhir_r4_us_core_3_1_0.inc.php:167` (list),
`_rest_routes_fhir_r4_us_core_3_1_0.inc.php:172` (read). Both routes use the
generic dispatcher: `new FhirGenericRestController($request, new FhirConditionService(), $globalsBag)`.

**Controller:** `OpenEMR\RestControllers\FHIR\FhirGenericRestController` with
service `OpenEMR\Services\FHIR\FhirConditionService` (route file line 61).

**Service:** `src/Services/FHIR/FhirConditionService.php`. Category search
support confirmed at lines 93–101.

| Inventory field | FHIR path                                | Notes |
|-----------------|------------------------------------------|-------|
| Problem title   | `code.text` (fall back `code.coding[0].display`) | Source: `lists.title`. ICD-10/SNOMED codings emitted when present. |
| Active filter (carry forward) | `clinicalStatus.coding[0].code` in [`active`, `recurrence`, `relapse`] | FHIR equivalent of inventory's `filterActiveIssues()`. |
| Category filter | `category=problem-list-item`             | Required to exclude `health-concern` and `encounter-diagnosis` categories. |

The card displays only the title; no date/status badge per `medical_problems.html.twig:17–21`.

## Medications

**Endpoint:** `GET /apis/default/fhir/MedicationRequest?patient={uuid}`

**Route:** `_rest_routes_fhir_r4_us_core_3_1_0.inc.php:470` (list),
`_rest_routes_fhir_r4_us_core_3_1_0.inc.php:482` (read).

**Controller:** `OpenEMR\RestControllers\FHIR\FhirMedicationRequestRestController`
(import at line 42).

**Service:** `src/Services/FHIR/FhirMedicationRequestService.php`. `intent` and
`status` are first-class search parameters (lines 113–114); `intent` is mapped
through `FHIRMedicationIntentEnum` (line 498) and `status` through
`FHIRMedicationStatusEnum` (line 510).

**Synthesis rule (carried forward from `dashboard-inventory.md` -> "Data source decision"):**

This card displays medications the patient is currently taking. Apply this
filter **client-side** on the bundle (rather than via query parameters) so we
can fall back gracefully if intent/status are not populated in synthetic data:

- `status` in {`active`, `on-hold`, `completed`}
- `intent` is **not filtered** — see `dashboard-inventory.md` →
  `## Medications` → "Synthesis rule" for why. Implementation:
  `filterMedications()` in `web/src/lib/fhir/synthesis.ts`.

| Inventory field | FHIR path                                            | Notes |
|-----------------|------------------------------------------------------|-------|
| Drug name       | `medicationCodeableConcept.text` (fallback `.coding[0].display`) | Service note line 164: only `medicationCodeableConcept` (not `medicationReference`) is supported. |
| Dosage line     | `dosageInstruction[0].text` (fallback `.patientInstruction` / structured `.doseAndRate`) | Inventory: `lists.drug_dosage_instructions`. TODO 1.5: confirm which dosage subfield OpenEMR populates for synthetic Furosemide / Lisinopril rows. |
| Status (filter) | `status`                                             | See synthesis rule. |
| Intent (filter) | `intent`                                             | See synthesis rule. |

**MedicationStatement gap (carried forward):** No `MedicationStatement`
controller in `src/RestControllers/FHIR/`, no `MedicationStatement` route in
the FHIR R4 routes file. ⚠ NOT IMPLEMENTED — the port synthesizes from
`MedicationRequest` per the inventory's documented compromise.

## Prescriptions

**Endpoint:** `GET /apis/default/fhir/MedicationRequest?patient={uuid}&intent=order&status=active`

**Route / Controller / Service:** same as Medications (above).

**Synthesis rule (carried forward):**

- `intent` = `order` (clinician-written prescriptions, not patient-reported)
- `status` = `active`
- `requester` populated (filter client-side on bundle)

| Inventory field | FHIR path                                            | Notes |
|-----------------|------------------------------------------------------|-------|
| Drug name       | `medicationCodeableConcept.text`                     | Same mapping as Medications. |
| Dosage / form / unit / route / interval | `dosageInstruction[0].text` and `.timing` / `.route` / `.doseAndRate` | Inventory's `prescriptions.dosage`/`.form`/`.unit`/`.route`/`.interval` collapse into FHIR's `dosageInstruction` shape. ⚠ One-to-one mapping NOT verified — TODO 1.5: confirm by curling a patient with real prescriptions. |
| Prescriber      | `requester.display` (or resolve `requester.reference`) | Empty for the synthetic dataset (Gloria has no prescriptions). |

⚠ **Open question for 1.5 verification (carried forward from inventory):**
whether OpenEMR's `MedicationRequest` output actually populates the `intent`
field with `order` for entries written through the prescribing workflow.
If not, fall back to category-based filtering (FHIR `category` =
`community` vs `inpatient`) or to a `requester`-not-null filter.

⚠ **Loaded-state shape unverified.** No synthetic patient has prescription
data (per inventory's "Synthetic dataset gaps"), so the loaded-row layout is
inferred from the FHIR resource shape alone. 1.5 should curl against a
non-synthetic OpenEMR instance — or seed a `prescriptions` row — before the
loaded UI is built.

## Care Team

**Endpoint:** `GET /apis/default/fhir/CareTeam?patient={uuid}&status=active`

**Route:** `_rest_routes_fhir_r4_us_core_3_1_0.inc.php:142` (list),
`_rest_routes_fhir_r4_us_core_3_1_0.inc.php:156` (read).

**Controller:** `OpenEMR\RestControllers\FHIR\FhirCareTeamRestController`
(import at line 29).

**Service:** `src/Services/FHIR/FhirCareTeamService.php`. `status` is a
search param (line 84). Role mapping (snake_case role -> SNOMED CT) at
lines 161–230; participant assembly at line 340.

| Inventory column | FHIR path                                                                         | Notes |
|------------------|-----------------------------------------------------------------------------------|-------|
| Team Name        | `name`                                                                            | Service line 137–138 sets this from `team_name` when present. |
| Team Status      | `status` (`active` / `inactive` / `proposed` / `suspended` / `entered-in-error`)  | Service line 127. |
| Type (Provider / Related Person) | `participant[*].member.type` (`Practitioner` vs `RelatedPerson`)    | ⚠ Inventory note: US Core 3.1.1 does NOT support RelatedPerson (service line 308 confirms). TODO 1.5: verify whether the running build emits RelatedPerson participants. |
| Member name      | `participant[*].member.display`                                                   | |
| Role             | `participant[*].role[0].text` / `.coding[0].display`                              | SNOMED CT mapping at service line 188. |
| Facility         | `participant[*].onBehalfOf.display` (organization participant)                    | TODO 1.5: confirm which CareTeam.participant the facility lives under (organization vs practitioner-bound). |
| Since            | `participant[*].period.start`                                                     | TODO 1.5: confirm OpenEMR populates `period.start` from `provider_since`. |
| Status (per-row) | `participant[*]` presence — FHIR has no per-participant status                    | ⚠ The inventory's per-row Status column has no direct FHIR equivalent. Open question: surface only team-level `status` in the port? |
| Note             | ⚠ NOT FOUND on `participant` in FHIR R4                                           | OpenEMR `member.note` does not have a standard FHIR mapping. TODO 1.5: drop the column from the port, or extend with a non-FHIR call. |

⚠ **Loaded-state shape inferred only.** No synthetic patient has Care Team
members (inventory's "Synthetic dataset gaps"). The FHIR row -> display
mapping above is built from the service code, not from a live response. 1.5
verification must seed a care team and curl this endpoint before the loaded
table layout is finalized.

## Vitals

**Endpoint:** `GET /apis/default/fhir/Observation?patient={uuid}&category=vital-signs`

For "most recent" semantics (the dashboard card shows only the latest row):
add `&_sort=-date&_count=1` per FHIR R4 search semantics. TODO 1.5: confirm
OpenEMR honours `_sort` and `_count` on Observation.

**Route:** `_rest_routes_fhir_r4_us_core_3_1_0.inc.php:493` (list),
`_rest_routes_fhir_r4_us_core_3_1_0.inc.php:498` (read).

**Controller:** `OpenEMR\RestControllers\FHIR\FhirGenericRestController` with
service `OpenEMR\Services\FHIR\FhirObservationService` (import at line 62).

**Service:** `src/Services/FHIR/FhirObservationService.php` dispatches into
sub-services. The vital-signs sub-service is
`src/Services/FHIR/Observation/FhirObservationVitalsService.php` —
`CATEGORY = "vital-signs"` (line 104), LOINC table at lines 87–264, OpenEMR
column mapping at lines 684–703.

**LOINC code map (verified against `FhirObservationVitalsService.php`):**

| Inventory field         | LOINC code | OpenEMR column (`form_vitals.*`) | FHIR path on the matched Observation |
|-------------------------|------------|----------------------------------|---------------------------------------|
| Blood Pressure (panel)  | `85354-9`  | `bps` + `bpd`                    | uses `component[*]` (no top-level `valueQuantity`) |
| BP systolic             | `8480-6`   | `bps`                            | `component[?(coding.code=='8480-6')].valueQuantity.value` (mmHg) |
| BP diastolic            | `8462-4`   | `bpd`                            | `component[?(coding.code=='8462-4')].valueQuantity.value` (mmHg) |
| Pulse                   | `8867-4`   | `pulse`                          | `valueQuantity.value` (per min) |
| Respiration             | `9279-1`   | `respiration`                    | `valueQuantity.value` (per min) |
| Temperature             | `8310-5`   | `temperature`                    | `valueQuantity.value` (degF or degC — TODO 1.5: confirm `valueQuantity.unit`/`code`. Inventory shows F displayed and C in parens, so the port may need to convert client-side.) |
| Temp Method             | ⚠ NOT FOUND in LOINC table | `temp_method`         | No standard LOINC; rendered in inventory as "Oral". TODO 1.5: check whether OpenEMR exposes via `Observation.method.text` or drops the field. |
| Oxygen Saturation       | `2708-6`   | `oxygen_saturation`              | `valueQuantity.value` (%) |
| Pulse Oximetry          | `59408-5`  | `oxygen_saturation` (combined in 7.0+, see service line 703) | `valueQuantity.value` (%) — backwards-compatible alias for `2708-6`. |
| Weight                  | `29463-7`  | `weight`                         | `valueQuantity.value` |
| Height                  | `8302-2`   | `height`                         | `valueQuantity.value` |
| BMI                     | `39156-5`  | `BMI`                            | `valueQuantity.value` |
| BMI percentile (peds)   | `59576-9`  | `ped_bmi`                        | `valueQuantity.value` (%) |
| Head Circumference      | `9843-4`   | `head_circ`                      | `valueQuantity.value` |
| Head Circ percentile (peds) | `8289-1` | `ped_head_circ`                | (peds) |
| Weight-for-Length pct (peds) | `77606-2` | `ped_weight_height`          | (peds) |
| Waist Circumference     | ⚠ NOT FOUND in LOINC table (vitals service) | `waist_circumference` | The inventory's "additional fields the dashboard does NOT display" list includes Waist Circumference. The vitals service constants do not enumerate a LOINC for it. TODO 1.5: confirm whether OpenEMR emits it as a non-vital-signs observation (e.g., LOINC `8280-0`) or drops it from the FHIR projection. |
| `form_vitals.date` ("Most recent vitals from") | n/a | `effectiveDateTime` |
| `form_vitals.last_updated` ("Last Updated")    | n/a | `meta.lastUpdated` (TODO 1.5: confirm) |

**Filter:** always include `category=vital-signs` per US Core profile
(`USCDI_PROFILE_VITAL_SIGNS` at service line 82). Without it the bundle would
include labs and survey observations.

**Card display rule (corrected 2026-05-08):** the original card is **not**
a curated 7-LOINC subset. `interface/forms/vitals/report.php:46–53`
iterates every column on the most-recent `form_vitals` row and renders
each non-empty column in a fixed presentation order. The seven rows on
Gloria's screenshot reflect which columns happen to be populated for her;
a different patient with `weight` / `height` / `BMI` populated would
render those rows on the same card.

**Port rule:**

1. Request all `vital-signs` Observations: `GET /Observation?patient={uuid}&category=vital-signs`.
2. Group entries by LOINC code, keeping the most recent observation per LOINC by `effectiveDateTime`.
3. Render rows in this fixed display order, skipping any LOINC for which no observation is present:
   1. Blood Pressure (`85354-9` panel; if absent, fall back to component synthesis from `8480-6` + `8462-4`)
   2. Temperature (`8310-5`)
   3. Pulse (`8867-4`)
   4. Respiration (`9279-1`)
   5. Oxygen Saturation (`2708-6` or `59408-5`)
   6. Height (`8302-2`)
   7. Weight (`29463-7`)
   8. BMI (`39156-5`)
   9. Head Circumference (`9843-4`)
   10. Waist Circumference (if surfaced as `8280-0` — see TODO 1.5; otherwise drop)
4. Prepend a header line: **"Most recent vitals from: {effectiveDateTime}"** sourced from the most recent observation across the bundle.
5. Append the trailing link: **"Click here to view and graph all vitals."**
6. Append a "Last Updated" footer row sourced from the most recent `meta.lastUpdated` across the rendered observations.

**Explicit cuts vs the original PHP card:**
- **Temp Method** (`form_vitals.temp_method`, e.g. "Oral") — has no LOINC mapping in `FhirObservationVitalsService` and is not exposed via the FHIR vital-signs endpoint. The port omits this row. Documented in `PATIENT_DASHBOARD_MIGRATION.md`.

The earlier "filter to seven LOINCs client-side" recommendation was an
invention by the api-map; it has no basis in the original PHP source.
Removed.

## Open questions for 1.5 verification

1. **`pid -> uuid` resolution.** Confirm the exact endpoint or session
   bootstrap path the SMART-launched dashboard uses to obtain a patient UUID
   from the legacy numeric `pid`. Likely candidates: the SMART `launch`
   parameter, or a one-shot `GET /apis/default/api/patient/{pid}` call.
2. **`MedicationRequest.intent` populated for OpenEMR-written prescriptions.**
   The Prescriptions card's entire synthesis rule depends on `intent=order`
   distinguishing prescriptions from medication-list items. If OpenEMR
   defaults all `MedicationRequest` rows to `plan` or `unknown`, the rule
   collapses and we need a fallback (probably `requester` non-null).
3. **`Patient.photo` for the avatar.** US Core 3.1.1 lists `photo` as
   "must support". Confirm whether OpenEMR's `FhirPatientService` populates it
   from the `documents` table or whether the port needs a separate
   non-FHIR call to render the avatar.
4. **Care Team — every field below team-level `status` and `name`.** No
   synthetic patient has a care team; the entire row mapping (Type, Member,
   Role, Facility, Since, Note) is inferred from the service code, not from
   a live response. Seed a care team and curl before the loaded UI is built.
5. **Prescriptions — `dosageInstruction` shape.** Inventory's five fields
   (dosage / form / unit / route / interval) collapse into FHIR's
   `dosageInstruction` array. Confirm whether OpenEMR emits structured
   `doseAndRate` / `timing` / `route` or a flattened `text` string only.
6. **Vitals — `Temp Method` and `Waist Circumference`.** Neither has an
   obvious LOINC in `FhirObservationVitalsService`. Decide whether to drop
   these columns, surface them via `Observation.method`, or call a
   non-FHIR endpoint.
7. **Empty-state distinction.** Allergies and Medical Problems each have a
   "touched" vs "untouched" empty state in OpenEMR. FHIR has no equivalent
   flag. Decide whether the port collapses both to a single empty message or
   keeps the distinction via a non-FHIR call.
8. **`_sort` / `_count` support on `/Observation`.** The Vitals card shows the
   most-recent row only; confirm OpenEMR honours `_sort=-date&_count=1`
   rather than the port having to fetch the whole bundle and sort client-side.

---

## 1.5 verification results (2026-05-07)

Verified live against `https://localhost:9300/apis/default/fhir` using a
password-grant access token (`auth-notes.md` captures the flow). Patient
under test: Gloria Tran, pid=4, uuid `a1af78de-c153-42e7-89b2-55749a3da9ca`.

### Resolved questions

- **Q1 — pid→uuid resolution:** `GET /Patient?identifier={pid}` returns a
  bundle of one with `entry[0].resource.id` = the FHIR uuid. Use this once
  at session start; no SMART launch parameter needed for direct dashboard
  navigation.
- **Q2 — `MedicationRequest.intent`:** ⚠ **Confirmed empirical gap.** Zero
  `MedicationRequest` rows across pids `{4, 5, 13, 24, 26, 27}` have
  `intent=order`. Every entry on this OpenEMR build emits `intent=plan`
  with `requester` absent. **The Prescriptions card's synthesis rule
  (`intent=order` AND `requester` recorded) returns empty for every
  synthetic patient.** This matches the original dashboard's behavior —
  Gloria's reference screenshot shows "None" — and is consistent with the
  inventory's hypothesis. Documenting as a known limitation in the
  migration doc, not a bug.
- **Q4 — CareTeam:** `GET /CareTeam?patient={uuid}` returns `total=0` for
  Gloria. Consistent with `care_teams`/`care_team_member` MySQL tables
  being empty across the synthetic dataset.

### Verified resource shapes

- **AllergyIntolerance:** `clinicalStatus.coding[0].code` and
  `verificationStatus.coding[0].code` populate. `criticality` is absent
  on Gloria's Sulfonamide record. **Allergen name lives in `text.div`**
  when `code.coding[0].code = unknown` (data-absent-reason), not in
  `code.text`. Port must fall back: prefer `code.coding[].display` if
  not "Unknown", else parse `text.div`. `category=["medication"]` works.
- **Condition (problem-list-item):** Heart failure for Gloria; `code.text`,
  `clinicalStatus.coding[0].code`, `onsetDateTime` all populate as the
  inventory expects.
- **Observation (vital-signs):** 15 entries for Gloria. LOINC codes
  populate as expected (`85353-1` panel, `9279-1` respiratory rate,
  `8867-4` heart rate). `valueQuantity.value` + `valueQuantity.unit`
  populate. `effectiveDateTime` populated.

### Still outstanding

- Q3 (Patient.photo for avatar) — not exercised in 1.5
- Q5 (dosageInstruction shape) — deferred to when a real prescription
  exists
- Q6 (Temp Method / Waist Circumference) — not in vital-signs bundle
- Q7 (empty-state touched-vs-untouched) — no FHIR flag exists
- Q8 (`_sort` / `_count` on Observation) — not exercised in 1.5
