# Dashboard Inventory

**Reference patient (primary):** Gloria Tran, MRN 4
**Reference patient (alternate):** Alejandro Cruz, MRN 24 (richer lab data)
**OpenEMR version:** 8.1.1-dev
**Captured:** 2026-05-06

## Scope

The OpenEMR patient dashboard surfaces approximately 18 sections. This port
covers six per the brief: five required + one additional.

**In scope (required):**
- Patient header
- Allergies
- Medical Problems (brief calls this "Problem List")
- Medications
- Prescriptions
- Care Team

**In scope (additional, my choice):** Vitals

**Out of scope (visible on original dashboard, not ported):**
Treatment Intervention Preferences, Care Experience Preferences,
Demographics, Billing, Insurance, Messages, Patient Reminders,
Disclosures, Amendments, Labs, Patient Portal / API Access,
Clinical Reminders, Recall, Appointments, Health Concerns, Immunizations

Out-of-scope sections are excluded per brief scope, not as omissions.
This decision is documented in PATIENT_DASHBOARD_MIGRATION.md.

## Synthetic dataset gaps

- `22-card-prescriptions-loaded.png` — No patient in synthetic dataset has
  Prescriptions (`prescriptions` table empty across all 27 patients). Loaded
  state inferred from FHIR `MedicationRequest` resource shape.
- `23-card-careteam-loaded.png` — No patient in synthetic dataset has Care
  Team members (`care_teams` and `care_team_member` tables empty across all
  27 patients). Loaded state inferred from FHIR `CareTeam` resource shape.

## Architecture finding (verified twice against codebase)

The current OpenEMR patient dashboard is server-rendered PHP with a hybrid
loading model. Findings verified by sub-agent investigation and confirmed
by an independent line-by-line spot-check.

### Rendering model

- **Entry point:** `interface/patient_file/summary/demographics.php`
- **AJAX-loaded fragments** (lines 526–533, via `placeHtml()`): vitals,
  labs, clinical reminders, and 6 others — total of 9 `*_fragment.php`
  files under `interface/patient_file/summary/`
- **Inline Twig rendering** (lines 1112–1208): allergies, medical
  problems, medications, prescriptions render directly in
  demographics.php via `$t->render('patient/card/*.html.twig', ...)`
- Both paths hit the database directly via `sqlQuery()` and service
  classes. No abstraction over the data layer.

### FHIR layer relationship

- The FHIR R4 API exists in parallel under `src/RestControllers/FHIR/`
  with controllers for Patient, AllergyIntolerance, CareTeam, Condition,
  MedicationRequest, Observation
- The dashboard does not consume any of these. Verified by grep across
  `interface/patient_file/summary/` — zero references to
  `/apis/default/fhir/` or any FHIR controller class
- The two FHIR-related imports in demographics.php are a SMART-launch
  app import (line 51) and a portal feature flag boolean (line 1578).
  Neither consumes the FHIR API.

### Confirmed gap: MedicationStatement

- No controller exists in `src/RestControllers/FHIR/`
- No route exists in `apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php`
- Decision: synthesize MedicationStatement from MedicationRequest using
  the `intent` and `status` fields to distinguish active medications
  from active prescriptions (Option B). Documented as an explicit
  compromise in PATIENT_DASHBOARD_MIGRATION.md.

### Implications for the port

1. Both fragment-based and inline-Twig cards must be ported to a single
   uniform card pattern in the new framework
2. Auth migration is real engineering work: session + CSRF → OAuth2/SMART
3. The "modernization" claim in the migration doc has concrete substance:
   decoupling presentation from server-rendering, unifying the hybrid
   loading model, and routing through a stable typed API
4. MedicationStatement requires explicit synthesis from MedicationRequest

References:
- Verification 1: sub-agent report on architecture claims
- Verification 2: spot-check of all 4 claims with line-by-line evidence

## Medications

### Data source decision

**Source endpoint:** `GET /apis/default/fhir/MedicationRequest?patient={id}`

**Synthesis rule:** This card displays medications the patient is
currently taking. Filter MedicationRequest results where:
- `status` is in [`active`, `on-hold`, `completed`] (excluding stopped,
  cancelled, entered-in-error)
- `intent` is `order` or `instance-order`

The "active medication list" view of the original dashboard (Furosemide
80mg IV, Lisinopril 10mg PO for Gloria) is reproduced from this
filtered set.

**Why not MedicationStatement:** This OpenEMR build does not implement
the MedicationStatement FHIR resource. No controller, no route. Verified
during the architecture audit (see "Architecture finding" above).
MedicationRequest is the only available FHIR resource for medication data.

**Compromise:** FHIR R4 distinguishes prescription orders
(MedicationRequest) from patient-reported medication usage
(MedicationStatement). With only MedicationRequest available, this
distinction collapses. The port treats both Medications and Prescriptions
cards as views of MedicationRequest filtered by different criteria.
This compromise is documented in PATIENT_DASHBOARD_MIGRATION.md.

## Prescriptions

### Data source decision

**Source endpoint:** `GET /apis/default/fhir/MedicationRequest?patient={id}`

**Synthesis rule:** This card displays prescriptions specifically written
through this OpenEMR system. Filter MedicationRequest results where:
- `intent` is `order` (prescriptions written by a clinician through
  OpenEMR's prescribing workflow)
- `status` is `active`
- A prescriber (`requester` field) is recorded

The empty state on Gloria ("None") indicates no MedicationRequests with
intent=order exist for this patient — the listed Furosemide and
Lisinopril likely have intent=`plan` or were entered without going
through the prescribing workflow.

**Open question for verification:** Need to confirm with `curl` against
a patient with actual prescriptions whether the intent/status filter
correctly distinguishes Prescriptions from the broader Medications
list. If not, fall back to a status+category-based filter.

**Why not MedicationStatement:** Same as above — not implemented in
this OpenEMR build.