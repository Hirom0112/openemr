# OpenEMR Patient Dashboard — Architecture Findings

Investigation of `/Users/hirom/Desktop/repos-gauntlet/openemr` to verify four
claims about the current patient dashboard before porting to a modern FHIR-
consuming frontend. All paths are absolute. Read-only investigation; no source
files were modified.

---

## 1. Claim: The dashboard renders server-side via PHP fragments — **CONFIRMED (with nuance)**

The dashboard entry point is
`/Users/hirom/Desktop/repos-gauntlet/openemr/interface/patient_file/summary/demographics.php`
(2,072 lines). It is a single monolithic PHP page that mixes server-rendered
HTML, inline JavaScript, and AJAX calls back into the same directory.

The page uses **two different rendering strategies**, both server-side PHP:

### (a) AJAX-injected fragments via `placeHtml()`

A helper `placeHtml(url, divId)` at lines 526–533 of `demographics.php` does
`fetch(url)` → `contentDiv.innerHTML = fragment`. The body of `$(function(){...})`
calls it for each card:

- line 609: `placeHtml("pnotes_fragment.php", 'pnotes_ps_expand')`
- line 624: `placeHtml("disc_fragment.php", "disclosures_ps_expand")`
- line 625: `placeHtml("labdata_fragment.php", "labdata_ps_expand")`
- line 626: `placeHtml("track_anything_fragment.php", "track_anything_ps_expand")`
- line 629: `placeHtml("vitals_fragment.php", "vitals_ps_expand")`
- line 633: `placeHtml("clinical_reminders_fragment.php", "clinical_reminders_ps_expand", true, true)`
- line 713: `placeHtml("patient_reminders_fragment.php", "patient_reminders_ps_expand", false, true)`
- line 727: `$(...).load("lbf_fragment.php?formname=...")` for layout-based forms

Fragment files found in
`/Users/hirom/Desktop/repos-gauntlet/openemr/interface/patient_file/summary/`:

- `add_edit_issue_medication_fragment.php`
- `clinical_reminders_fragment.php`
- `disc_fragment.php`
- `labdata_fragment.php`
- `lbf_fragment.php`
- `patient_reminders_fragment.php`
- `pnotes_fragment.php`
- `track_anything_fragment.php`
- `vitals_fragment.php`

### (b) Direct in-page Twig rendering (NOT a fragment)

Allergies, Medical Problems, Medications, Prescriptions, and Care Team are
**not** loaded as separate `*_fragment.php` files. They are rendered inline in
`demographics.php` itself by calling service classes synchronously and piping
the data through Twig templates. Examples (`demographics.php`):

- lines 1112–1133 — Allergy card uses `AllergyIntoleranceService::getAll()` then
  `$t->render('patient/card/allergies.html.twig', $viewArgs)`.
- lines 1137–1157 — Medical Problems card via `PatientIssuesService::search()`
  + `patient/card/medical_problems.html.twig`.
- lines 1159–1179 — Medications card same pattern + `patient/card/medication.html.twig`.
- lines 1181–1208 — Prescriptions card (raw SQL `SELECT * FROM prescriptions`
  at line 1184) + `patient/card/erx.html.twig`.

**Nuance:** the claim "each card corresponds to a `*_fragment.php`" is **only
partially true**. The pattern is "server-rendered HTML via PHP/Twig", but the
mechanism is split: some cards are AJAX-loaded fragments, while the most
important clinical cards (allergies, problems, meds, rx) are rendered inline in
the parent page using direct service-layer calls and Twig templates, not via a
`*_fragment.php` round-trip.

---

## 2. Claim: Fragment files return HTML, not JSON — **CONFIRMED**

All inspected fragments emit HTML directly (echoes / inline PHP-in-HTML / Twig
includes). None call `json_encode()` for their primary output. `grep` for
`json_encode` across `vitals_fragment.php`, `labdata_fragment.php`,
`clinical_reminders_fragment.php`, `pnotes_fragment.php`, and
`disc_fragment.php` returned **no matches**.

### Evidence — `vitals_fragment.php` (full file is 49 lines)

```php
// /Users/hirom/Desktop/repos-gauntlet/openemr/interface/patient_file/summary/vitals_fragment.php
22: ?>
23: <div id='vitals''><!--outer div-->
24: <?php
25: //retrieve most recent set of vitals.
26: $result = sqlQuery("SELECT FORM_VITALS.date, FORM_VITALS.id FROM form_vitals ...", [$pid]);
...
30:   <span class='text'> <?php echo xlt("No vitals have been documented."); ?>
...
41:     <?php include_once(... "/forms/vitals/report.php");
42:     vitals_report('', '', 1, $result['id']);
```

Direct DB query, then HTML emitted via `?>` literal markup, `echo xlt(...)`,
and an `include_once` of another PHP report file. No JSON.

### Evidence — `labdata_fragment.php` (full file is 63 lines)

```php
// /Users/hirom/Desktop/repos-gauntlet/openemr/interface/patient_file/summary/labdata_fragment.php
24: <div id='labdata' style='margin-top: 3px; margin-left: 10px; margin-right: 10px'>
...
51:     echo xlt('Procedure') . ": " . text($result['theprocedure']) . " (" . text($result['thedate']) . ")<br />";
52:     echo xlt('Encounter') . ": <a href='../../patient_file/encounter/encounter_top.php?set_encounter=" . attr_url($result['theencounter']) . "' target='RBot'>" . text($result['theencounter']) . "</a>";
```

Plain HTML strings via `echo`. No serialization.

### Evidence — `clinical_reminders_fragment.php` (full file is 24 lines)

```php
// /Users/hirom/Desktop/repos-gauntlet/openemr/interface/patient_file/summary/clinical_reminders_fragment.php
22:
23: clinical_summary_widget($pid, "reminders-due", '', 'default', $session->get('authUser'));
```

Delegates to `clinical_summary_widget()` (from `library/clinical_rules.php`)
which prints HTML directly to stdout. No REST controller, no JSON.

All fragments perform CSRF check (`CsrfUtils::checkCsrfInput(INPUT_POST, dieOnFail: true)`)
and read `$pid` from session/globals — they are NOT REST endpoints; they are
authenticated HTML partials over the standard PHP session.

---

## 3. Claim: OpenEMR has FHIR endpoints covering the same data — **CONFIRMED**

FHIR controllers live in
`/Users/hirom/Desktop/repos-gauntlet/openemr/src/RestControllers/FHIR/`. The
route table is in
`/Users/hirom/Desktop/repos-gauntlet/openemr/apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php`
(loaded by the top-level `/Users/hirom/Desktop/repos-gauntlet/openemr/_rest_routes.inc.php`).

Mapping for the resources requested:

| FHIR Resource         | Route (method + path)                                              | Controller / handler file                                                                                                       | Route line |
|-----------------------|--------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------|-----------:|
| Patient               | `GET /fhir/Patient`, `GET /fhir/Patient/:uuid`, `POST`, `PUT`      | `src/RestControllers/FHIR/FhirPatientRestController.php`                                                                        | 560–610    |
| AllergyIntolerance    | `GET /fhir/AllergyIntolerance`, `GET /fhir/AllergyIntolerance/:uuid` | `src/RestControllers/FHIR/FhirAllergyIntoleranceRestController.php`                                                             | 73, 85     |
| Condition             | `GET /fhir/Condition`, `GET /fhir/Condition/:uuid`                 | `src/RestControllers/FHIR/FhirGenericRestController.php` wrapping `FhirConditionService` (lines 168, 173)                       | 167, 172   |
| MedicationRequest     | `GET /fhir/MedicationRequest`, `GET /fhir/MedicationRequest/:uuid` | `src/RestControllers/FHIR/FhirMedicationRequestRestController.php`                                                              | 470, 482   |
| MedicationStatement   | **NOT IMPLEMENTED**                                                | No `FhirMedicationStatement*` file in `src/RestControllers/FHIR/`; no `MedicationStatement` reference in `_rest_routes_fhir_r4_us_core_3_1_0.inc.php` | n/a        |
| CareTeam              | `GET /fhir/CareTeam`, `GET /fhir/CareTeam/:uuid`                   | `src/RestControllers/FHIR/FhirCareTeamRestController.php`                                                                       | 142, 156   |
| Observation           | `GET /fhir/Observation`, `GET /fhir/Observation/:uuid`             | `src/RestControllers/FHIR/FhirObservationRestController.php` (vitals + labs both surface here)                                  | 493, 498   |

Additionally available and likely useful for the migration:
`FhirMedicationDispenseRestController.php`, `FhirImmunizationRestController.php`,
`FhirEncounterRestController.php`, `FhirProcedureRestController.php`,
`FhirDocumentReferenceRestController.php`, `FhirGoalRestController.php`,
`FhirCarePlanRestController.php`, `FhirDiagnosticReportRestController.php`.

**Nuance:** **MedicationStatement is missing.** OpenEMR exposes
`MedicationRequest` (active prescriptions) and `MedicationDispense`, but no
`MedicationStatement` resource. If the new dashboard genuinely needs
MedicationStatement semantics (patient-reported / non-prescribed meds), it
will have to derive them from `MedicationRequest` + the legacy `lists` table or
add a new FHIR controller.

---

## 4. Claim: The dashboard does not consume the FHIR API — **CONFIRMED**

`grep` for `/apis/default/fhir`, `/apis/fhir`, `FhirPatientRestController`,
`FhirAllergy`, `FhirCondition`, `FhirMedication`, `FhirCareTeam`, and
`FhirObservation` across
`/Users/hirom/Desktop/repos-gauntlet/openemr/interface/patient_file/summary/`
and `/Users/hirom/Desktop/repos-gauntlet/openemr/interface/patient_file/` and
`/Users/hirom/Desktop/repos-gauntlet/openemr/interface/main/` returned **no
matches** — the dashboard never fetches from a FHIR endpoint or instantiates a
FHIR controller.

Only two `fhir`-related references appear in
`demographics.php`:

- line 51: `use OpenEMR\FHIR\SMART\SmartLaunchController;` — used for SMART-on-
  FHIR app launch buttons (a separate concern from rendering dashboard data).
- line 1578: `OEGlobalsBag::getInstance()->get('rest_fhir_api')` — a feature-
  flag check used only to decide whether to render a "Portal Access" card; it
  does not call the FHIR API.

How dashboard data is actually fetched:

- Direct SQL via `sqlQuery(...)` / `sqlStatement(...)` in fragments
  (`vitals_fragment.php` line 26; `labdata_fragment.php` line 36;
  `demographics.php` line 1184 for prescriptions).
- Direct service-layer calls bypassing REST: `AllergyIntoleranceService::getAll()`
  (line 1115), `PatientIssuesService::search()` (lines 1139, 1161). These are
  the same services the FHIR controllers wrap, but the dashboard calls them in-
  process — no HTTP, no FHIR serialization, no auth scopes.
- AJAX calls to sibling PHP scripts that also return HTML
  (`soap_patientfullmedication.php`, `soap_allergy.php` — `dataType: "html"` at
  lines 553, 573 of `demographics.php`).

The FHIR layer therefore exists in **parallel** with the dashboard, sharing the
underlying service classes but never being invoked by the dashboard UI.

---

## 5. Implications for the migration doc

The FHIR API is a viable backend for a modern dashboard for every required
card except **MedicationStatement**, which has no controller today and will
need to be sourced from `MedicationRequest` (and possibly the legacy `lists`
table for patient-reported meds) or implemented as a new FHIR resource. The
existing dashboard does not consume FHIR at all — it renders HTML server-side
via a mix of AJAX-loaded `*_fragment.php` partials (vitals, labs, pnotes,
disclosures, clinical reminders, patient reminders, track-anything, LBF forms)
and **inline Twig templates rendered directly inside `demographics.php`** for
the most important clinical cards (allergies, problems, medications,
prescriptions, care team), with both paths reaching into raw SQL and the
service layer in-process. Migration must therefore (a) re-implement the inline
Twig cards, not just the `*_fragment.php` files, since they hold the bulk of
the clinical surface; (b) add a MedicationStatement story before claiming
parity; (c) replace CSRF-protected form-session HTML partials with token-
authenticated FHIR JSON, which is a security-model change the new frontend
must handle (OAuth2/SMART scopes vs. the current PHP session + CSRF).
