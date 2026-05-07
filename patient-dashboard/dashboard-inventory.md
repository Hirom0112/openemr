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

## Patient header

### Position

Top of page, full width — rendered above the two-column body by the
`OemrUI` chrome. Not a column-bound card; it is the page-level
identity banner shown for every patient view, not just the dashboard.

### Fields

| Field            | Source (synthesized)                                  | Notes |
|------------------|--------------------------------------------------------|-------|
| Patient name     | `patient_data.fname` + `lname` (link to dashboard)     | Rendered as a link with the patient name |
| MRN / pubpid     | `patient_data.pubpid` (shown in parentheses)           | "(4)" for Gloria Tran |
| Close button     | "x" icon — closes the patient context                  | TODO: confirm exact target — likely clears `pid` session and returns to finder |
| Patient avatar   | Placeholder silhouette when no photo on file           | Backed by `pic_array($pid, ...)` patient-photo category (demographics.php:1634) |
| DOB              | `patient_data.DOB`, formatted `YYYY-MM-DD`             | "DOB: 1958-09-03" |
| Age              | Computed from DOB                                      | "Age: 67" |
| Encounter picker | Recent-encounters icon + "Select Encounter (N)" select | N is the count of encounters; opens dropdown to pick one |
| Open Encounter   | Label "Open Encounter: None" or encounter date         | "None" when no open encounter |
| New encounter    | "+" button — creates a new encounter                   | TODO: confirm target URL |

### States

- **Loaded:** `reference-screenshots/10-card-header.png` — Gloria Tran (4),
  DOB 1958-09-03, Age 67, "Select Encounter (1)", "Open Encounter: None"
- No empty/loading state — the header is server-rendered with the page
  and is always populated for a valid `pid`

### Edit affordance

The header has no pencil icon. The patient name is a link (back to the
dashboard / patient summary), and the "x" closes the patient context.
Demographic editing happens through the Demographics card (out of
scope) in the right column.

### Source in current code

- Header chrome built by `OemrUI` constructed at
  `interface/patient_file/summary/demographics.php:363–375` with
  `'include_patient_name' => true` (line 366)
- Avatar / photo data: `pic_array($pid, OEGlobalsBag::getInstance()->getString('patient_photo_category_name'))`
  at `demographics.php:1634`
- Underlying patient identity row (`$result`) loaded earlier in
  `demographics.php` from `patient_data` and passed into
  `DemographicsViewCard` at `demographics.php:1336`
- TODO: confirm the exact Twig partial / `OemrUI` template that emits
  the name banner (`include_patient_name` is consumed inside
  `OpenEMR\OeUI\OemrUI` — not yet traced in this pass)

## Allergies

### Position

Left column (`col-md-8`, `demographics.php:1327`), inline-Twig card
group. Rendered in the first `.row` of the dashboard body alongside
Medical Problems, Medications, and Prescriptions, each wrapped in a
flex column (`flex-fill mx-1 card`) so all four sit side-by-side
within the left column.

### Fields

| Field          | Twig binding (template var)            | Source field |
|----------------|-----------------------------------------|--------------|
| Card title     | `title`                                 | `xl('Allergies')` literal |
| Allergen name  | `l.title`                               | `lists.title` (allergy issue title) |
| Reaction       | `l.reaction_title` (in `title=` tooltip) | `lists.reaction` joined to list option label |
| Severity label | `l.severity_al` resolved via `getListItemTitle('severity_ccda', l.severity_al)` | `lists.severity_al` |
| Severity flag  | Severity in `["severe","life_threatening_severity","fatal"]` triggers `bg-warning font-weight-bold` highlight | derived |

Display format per row: `{{ allergen }} ({{ severity }})`. Severity in
parentheses; the reaction surfaces only as the row's hover tooltip.

### States

- **Loaded:** `reference-screenshots/11-card-allergies-loaded.png` —
  "Sulfonamide ()" (Gloria Tran has a Sulfonamide allergy with no
  recorded severity, so the parens are empty)
- **Empty (touched):** `reference-screenshots/21-card-allergies-empty.png`
  — "No Known Allergies" (rendered when `listTouched` is true and the
  list is empty; see `allergies.html.twig:27–30`)
- **Empty (untouched):** "Nothing Recorded" (rendered when the list
  is empty *and* `listTouched` is false; `allergies.html.twig:32–35`).
  TODO: confirm — no screenshot of this state in the captured set.
- No loading state — content is server-rendered with the page.

### Edit affordance

Pencil icon → JavaScript `load_location(...)` to
`/interface/patient_file/summary/stats_full.php?active=all&category=allergy`
(`demographics.php:1128`). See
`reference-screenshots/40-edit-allergies.png` (Medical Issues page,
"Allergies" section, with the entry "Sulfonamide (Active) — Unassigned
— Occurrence Unknown or N/A").

### Source in current code

- Render block: `interface/patient_file/summary/demographics.php:1112–1133`
- Data source: `OpenEMR\Services\AllergyIntoleranceService->getAll(['lists.pid' => $pid])`
  filtered by local `filterActiveIssues()` (excludes outcome=resolved
  and end-dated issues; demographics.php:1107–1110, 1115)
- Template: `templates/patient/card/allergies.html.twig`
- Card chrome: extends `templates/patient/card/card_base.html.twig`

## Medical Problems

### Position

Left column (`col-md-8`), inline-Twig card. Sits second among the
four left-column inline cards (allergies → problems → medications →
prescriptions).

### Fields

| Field         | Twig binding | Source field |
|---------------|--------------|--------------|
| Card title    | `title`      | `xl('Medical Problems')` |
| Problem title | `l.title`    | `lists.title` (issue title; ICD-10 / SNOMED-derived) |

`medical_problems.html.twig:17–21` renders only the title — no
date, status, or code is shown on the card.

### States

- **Loaded:** `reference-screenshots/12-card-problems-loaded.png` —
  "Heart failure" (single-line entry, no badge or date)
- **Empty (touched):** "None" (template literal `"None{{Issues}}"`,
  `medical_problems.html.twig:8–10`)
- **Empty (untouched):** "Nothing Recorded" (`medical_problems.html.twig:12–14`).
  TODO: confirm — no screenshot captured.

### Edit affordance

Pencil icon → `load_location(...)` to
`stats_full.php?active=all&category=medical_problem`
(`demographics.php:1152`). See
`reference-screenshots/41-edit-problems.png` (Medical Issues page,
"Medical Problems" section, "Heart failure (Active) — Occurrence
Unknown or N/A").

### Source in current code

- Render block: `demographics.php:1135–1157`
- Data source: `OpenEMR\Services\PatientIssuesService->search(['lists.pid' => $pid, 'lists.type' => 'medical_problem'])`
  filtered by `filterActiveIssues()` (demographics.php:1139, 1148)
- Template: `templates/patient/card/medical_problems.html.twig`

## Medications

### Position

Left column (`col-md-8`), inline-Twig card. Third in the left-column
inline-card group.

### Fields

| Field        | Twig binding               | Source field |
|--------------|----------------------------|--------------|
| Card title   | `title`                    | `xl('Medications')` |
| Drug name    | `m.title`                  | `lists.title` |
| Dosage line  | `m.drug_dosage_instructions` | `lists.drug_dosage_instructions` |

`medication.html.twig:17–22` renders title + dosage on a single
line. The screenshot shows the two collapsed into one display string
("Furosemide 80mg IV", "Lisinopril 10mg PO") — TODO: confirm whether
that whole string lives in `lists.title` for synthetic data or
whether dosage_instructions contributes the "80mg IV" / "10mg PO"
half. The Medical Issues edit page shows just "Furosemide 80mg IV"
as the title, suggesting the entire string is in `lists.title` for
this dataset.

### States

- **Loaded:** `reference-screenshots/13-card-medications-loaded.png`
  — two rows: "Furosemide 80mg IV", "Lisinopril 10mg PO"
- **Empty (touched):** "None" (same pattern as Medical Problems)
- **Empty (untouched):** "Nothing Recorded"
- TODO: confirm — no captured screenshot of either empty state.

### Edit affordance

Pencil icon → `load_location(...)` to
`stats_full.php?active=all&category=medication`
(`demographics.php:1174`). See
`reference-screenshots/42-edit-medications.png` (Medical Issues
page, "Medications" section, with both Furosemide and Lisinopril
listed as Active).

### Source in current code

- Render block: `demographics.php:1159–1179`
- Data source: `PatientIssuesService->search(['lists.pid' => $pid, 'lists.type' => 'medication'])`
  filtered by `filterActiveIssues()` (demographics.php:1161, 1170)
- Template: `templates/patient/card/medication.html.twig`

### Data source decision

**Source endpoint:** `GET /apis/default/fhir/MedicationRequest?patient={id}`

**Synthesis rule:** This card displays medications the patient is
currently taking. Filter MedicationRequest results where:
- `status` is in [`active`, `on-hold`, `completed`] (excluding stopped,
  cancelled, entered-in-error)
- `intent` is **not filtered** — the card shows everything currently
  in effect regardless of how it was entered. (Earlier draft proposed
  filtering on `intent in {order, instance-order}`; that was relaxed
  after 1.5 confirmed every synthetic record arrives with
  `intent=plan`. Filtering on intent at this layer would empty the
  Medications card too.) Implementation: `filterMedications()` in
  `web/src/lib/fhir/synthesis.ts`.

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

### Position

Left column (`col-md-8`), inline-Twig card, fourth in the
inline-card group. Wrapped in `<div class='col m-0 p-0 mx-1'>`
rather than `flex-fill mx-1 card`, so its column container differs
slightly from the first three inline cards
(`demographics.php:1240`).

### Fields

The body content is delegated to a legacy Smarty fragment captured
via output buffering:

```php
$c = new Controller();
ob_start();
echo $c->dispatch(['controller' => 'prescription', 'action' => 'fragment', 'patient_id' => $pid]);
$viewArgs['content'] = ob_get_contents();
ob_end_clean();
```
(`demographics.php:1231–1238`)

| Field        | Source                                                |
|--------------|-------------------------------------------------------|
| Card title   | `xl('Prescriptions')` (or `'Prescription History'` when eRx is enabled) |
| Drug name    | `prescriptions.drug` |
| Dosage / form / unit / route / interval | `prescriptions.dosage`, `.form`, `.unit`, `.route`, `.interval` (resolved through `generate_display_field()` against `drug_*` list_options for the eRx variant — demographics.php:1189–1192) |

TODO: confirm — the loaded shape rendered by the prescription Smarty
fragment was not inspected directly in this pass; the fields above
are inferred from the `prescriptions` table columns hydrated for the
parallel eRx code path (demographics.php:1184–1194).

### States

- **Empty:** `reference-screenshots/14-card-prescriptions-empty.png`
  — single line "None". This is the state for Gloria Tran (and every
  patient in the synthetic dataset; see "Synthetic dataset gaps"
  above).
- **Loaded:** TODO: confirm — no synthetic patient has prescriptions
  data. Loaded shape inferred from FHIR `MedicationRequest` per the
  data-source decision below.

### Edit affordance

Pencil icon → opens an iframe modal via
`editScripts('controller.php?prescription&list&id={pid}')`
(`demographics.php:1224`, `btnClass: "iframe"`). Opens the
prescription list management modal. See
`reference-screenshots/43-edit-prescriptions.png` — modal labelled
"There are currently no prescriptions." with `Add` and `Quit`
buttons; the "x" close button is the standard modal dismissal.

When eRx is enabled the affordance changes: button label becomes
`Add` and links to `/interface/eRx.php?page=compose`
(demographics.php:1219–1222). The eRx variant is out of scope for
this synthetic dataset.

### Source in current code

- Render block: `demographics.php:1181–1243`
- Twig wrapper template: `templates/patient/card/rx.html.twig`
- Body content: dispatched from `Controller` (legacy Smarty
  prescription fragment) — TODO: confirm exact file path under
  `controllers/`
- Underlying table: `prescriptions` (filtered to `active = '1'` in
  the eRx code path, demographics.php:1184)

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

**Empirically verified (1.5, 2026-05-07):** Across pids 4, 5, 13, 24, 26, 27,
**zero `MedicationRequest` rows return for `intent=order`**. Every entry in
this OpenEMR build emits `intent=plan` with `requester` absent. The
Prescriptions filter as drafted will return empty for every synthetic
patient — which matches the original dashboard's behavior (Gloria's
screenshot shows "None"). This is documented as a known limitation in
the migration doc, not a bug.

**Why not MedicationStatement:** Same as above — not implemented in
this OpenEMR build.

## Care Team

### Position

Full-width row beneath the four left-column inline cards
(`<div class='col-12 m-0 p-0 px-2'>`, `demographics.php:1268`).
Spans the full body width — does not sit inside the `col-md-8` /
`col-md-4` two-column split. Lives in the second `.row` of the
dashboard body, before Treatment Intervention Preferences and Care
Experience Preferences.

### Fields

Card body is a table with one row per care-team member. Columns from
the table header (`manage_care_team.html.twig:191–202`):

| Column          | Source field (per `CareTeamViewCard::getTemplateVariables()`) |
|-----------------|----------------------------------------------------------------|
| Type            | `member.member_type` ("Provider"/"Related Person" badge; `'user'` vs `'contact'`) |
| Member          | `member.user_display` / `member.contact_name` |
| Role            | `member.role_display` (resolved from `member.role`) |
| Facility        | `member.facility_display` (resolved from `member.facility_id`) |
| Since           | `member.provider_since` (date) |
| Status          | `member.status_display` (resolved from `member.status`) |
| Note            | `member.note` |
| Remove          | Edit-mode-only button (× to remove the row) |

Above the table the card also exposes:
- `team_name` — Care Team Name input (text); displayed as `<h5>` in
  view mode
- `team_status` / `team_status_display` — Care Team Status (Active /
  Inactive / etc.); displayed as a badge next to the name in view
  mode (`manage_care_team.html.twig:175–180`)

### States

- **Empty:** `reference-screenshots/15-card-careteam-empty.png` —
  table header rendered with no rows below it. No "Nothing
  Recorded" placeholder; the empty body simply shows the column
  headings (Type, Member, Role, Facility, Since, Status, Note,
  Remove). This is the state for every patient in the synthetic
  dataset (see "Synthetic dataset gaps").
- **Loaded:** TODO: confirm — no synthetic patient has Care Team
  members. Loaded row shape inferred from
  `CareTeamViewCard::getTemplateVariables()` (rows hydrated via the
  `existingCareTeam` JS array, populated row-by-row in
  `preloadTeamRows()` at `manage_care_team.html.twig:31–107`).

### Edit affordance

Pencil icon → toggles edit mode in-place via the card's
`btn-edit-care-team` handler (`manage_care_team.html.twig:157`).
The pencil button label is "Add" when no team exists yet, "Edit"
when a team exists (`demographics.php:1250–1255`). See
`reference-screenshots/44-edit-careteam.png` — Care Team Name
input, Care Team Status select (Active), and the row table with
"Add Team Member", "Add Related Person", "Save Care Team",
"Cancel" buttons revealed below.

Unlike the four inline cards above, the edit experience does not
navigate away — the card flips between view and edit modes within
the same template (`toggleEditMode()`,
`manage_care_team.html.twig:17–21`).

### Source in current code

- Render block: `interface/patient_file/summary/demographics.php:1247–1272`
- Card class: `OpenEMR\Patient\Cards\CareTeamViewCard`
  (`src/Patient/Cards/CareTeamViewCard.php`); imported at
  `demographics.php:56`. `getTemplateVariables()` lives at
  `CareTeamViewCard.php:112`+ and assembles the `existing_care_team`,
  `user_options`, `related_person_options`, `facility_options`,
  `role_options`, `status_options` arrays
- Template: `templates/patient/card/manage_care_team.html.twig`
- Underlying tables: `care_teams` and `care_team_member` (both empty
  in the synthetic dataset)
- TODO: confirm — exact SQL inside `CareTeamViewCard` for hydrating
  `existing_care_team` was not traced in this pass

## Vitals

### Position

Right column (`col-md-4`, `demographics.php:1573`). Rendered into
the secondary column near the bottom, below Labs and above
LBF-charted forms (`demographics.php:1503–1526`). Card chrome is
created by `patient/card/loader.html.twig` and the body is loaded
via AJAX after page render.

### Fields

Body content is produced by the legacy `vitals_report()` function
(`forms/vitals/report.php`, included from `vitals_fragment.php:41`).
Visible labelled fields per the screenshot:

| Field             | Source (`form_vitals.*` columns where identifiable) |
|-------------------|------------------------------------------------------|
| Most recent vitals from | `form_vitals.date` (timestamp) |
| Blood Pressure    | `form_vitals.bps` / `bpd` (rendered as `bps/bpd`) |
| Temperature       | `form_vitals.temperature` (F shown, C in parens) |
| Temp Method       | `form_vitals.temp_method` (e.g. "Oral") |
| Pulse             | `form_vitals.pulse` ("per min" suffix) |
| Respiration       | `form_vitals.respiration` ("per min" suffix) |
| Oxygen Saturation | `form_vitals.oxygen_saturation` ("%" suffix) |
| Last Updated      | `form_vitals.last_updated` (or `forms.date` of last edit; TODO: confirm — both candidates exist on the table) |

Additional fields the underlying `form_vitals` row carries — but
which the dashboard card itself does **not** display in either
captured screenshot: Weight, Height/Length, Head Circumference,
Waist Circumference, BMI, BMI Status, Inhaled Oxygen Concentration,
Oxygen Flow Rate, Temp Location, Other Notes. These appear on the
expanded "Vitals" form view (`reference-screenshots/45-edit-vitals.png.png`)
but the dashboard card body shows only the seven rows above.
TODO: confirm whether `vitals_report()` filters the displayed set or
whether the captured screenshot reflects nullable fields collapsing.

Trailing link: "Click here to view and graph all vitals." →
`/interface/encounter/trend_form.php?formname=vitals`
(`vitals_fragment.php:45`).

### States

- **Loaded (Gloria):** `reference-screenshots/16-card-vitals-loaded.png`
  — Most recent vitals from 2026-04-29 05:45:00, BP 156/94, Temp
  37 F (2.78 C), Oral, Pulse 96, Respiration 18, O2 94%, Last
  Updated 2026-05-02 19:29:52
- **Loaded (Alejandro):** `reference-screenshots/24-card-vitals-loaded-alejandro.png`
  — captured via the encounter "Vitals" form view (richer set with
  Weight, Height, Head/Waist circumference, BMI, etc.), not the
  dashboard card. Provided as evidence that the underlying form
  carries more fields than the dashboard card surfaces.
- **Empty:** "No vitals have been documented." (rendered when
  `sqlQuery(... form_vitals ... ORDER BY date DESC)` returns no
  row; `vitals_fragment.php:28–32`). TODO: confirm — no captured
  screenshot of the empty state.
- **Loading:** Card chrome renders immediately with a placeholder
  body (`patient/card/loader.html.twig`); the body is fetched by
  the page-level `placeHtml("vitals_fragment.php", "vitals_ps_expand")`
  call (`demographics.php:629`) and injected into
  `#vitals_ps_expand` after page load. Network response captured in
  `reference-screenshots/54-network-vitals-response.png .png` and
  the page-level fetch waterfall in
  `reference-screenshots/50-network-dashboard-load.png`.

### Edit affordance

The dashboard card uses `btnLabel: 'Trend'` rather than `Edit`; the
button links to
`../encounter/trend_form.php?formname=vitals&context=dashboard`
(`demographics.php:1515–1517`) — i.e. the Vitals trend graph, not
an editor. See `reference-screenshots/45-edit-vitals.png.png` for
the linked form view (full Vitals + Vitals History tables on
Alejandro). Adding/editing a vitals row is done through the
encounter form, not from the dashboard card itself. TODO: confirm
that there is no pencil-icon affordance on the dashboard Vitals
card (the captured screenshot shows none — only the collapse
toggle).

### Source in current code

- Card chrome wired up at
  `interface/patient_file/summary/demographics.php:1503–1526`
  (auth check, `existVitals` query, `loader.html.twig` render)
- AJAX trigger: `demographics.php:629`
  (`placeHtml("vitals_fragment.php", "vitals_ps_expand")`)
- Fragment: `interface/patient_file/summary/vitals_fragment.php`
  (CSRF check at line 20, most-recent query at line 26, body via
  `vitals_report()` from `forms/vitals/report.php` at line 41–42)
- Card chrome template: `templates/patient/card/loader.html.twig`
- Underlying table: `form_vitals` (joined to `forms` to filter
  `deleted != '1'`)