# Parity Audit — Patient Dashboard Port

Compares the 7 ported card components against the inventory spec
(`dashboard-inventory.md`), the API map (`dashboard-api-map.md`), and the
original OpenEMR dashboard's reference screenshots
(`patient-dashboard/reference-screenshots/`). Each row of each card's table
is one of:

- ✅ **parity** — port renders the same field with equivalent semantics
- ⚠ **deviation** — port behavior differs from original (must be defended in migration doc or fixed)
- ➕ **port-only** — port adds a field/state the original doesn't have
- ➖ **missing** — original has it; port doesn't
- 🔒 **out of scope** — original has it; brief excludes it (e.g., mutation affordances)
- 🟦 **synthetic-data caveat** — empty/unrenderable due to seed data, not a port bug

Cards reviewed (under `patient-dashboard/web/src/components/cards/`):
`PatientHeader.tsx`, `AllergiesCard.tsx`, `MedicalProblemsCard.tsx`,
`MedicationsCard.tsx`, `PrescriptionsCard.tsx`, `CareTeamCard.tsx`,
`VitalsCard.tsx`.

## Summary

| Card             | ✅ Parity | ⚠ Deviation | ➕ Port-only | ➖ Missing | 🔒 OOS | 🟦 Caveat |
|------------------|----------|-------------|-------------|-----------|--------|-----------|
| Patient header   | 4        | 1           | 0           | 0         | 3      | 1         |
| Allergies        | 4        | 2           | 1           | 0         | 1      | 0         |
| Medical Problems | 2        | 0           | 2           | 0         | 2      | 0         |
| Medications      | 2        | 1           | 1           | 0         | 2      | 0         |
| Prescriptions    | 1        | 0           | 0           | 0         | 2      | 3         |
| Care Team        | 2        | 1           | 0           | 4         | 2      | 4         |
| Vitals           | 5        | 4           | 0           | 0         | 1      | 1         |
| **Totals**       | **20**   | **9**       | **4**       | **4**     | **13** | **9**     |

## Patient header

Source: `PatientHeader.tsx`. Inventory: `dashboard-inventory.md → ## Patient header → Fields`.
Reference: `reference-screenshots/10-card-header.png`.

| Field/state         | Original                                         | Port                                                                                     | Status | Notes |
|---------------------|--------------------------------------------------|------------------------------------------------------------------------------------------|--------|-------|
| Patient name        | `fname` + `lname`, rendered as a link to dashboard | `formatPatientName()` `PatientHeader.tsx:41-61`; rendered as a `<span>` `PatientHeader.tsx:158-163` | ⚠ | Deviation: original is a link; port renders a span. Port also formats `family, given1 given2` (last-first) vs. original's "Gloria Tran" first-last in screenshot 10. |
| MRN / pubpid        | `(4)` in parens after name                        | `({patientId})` `PatientHeader.tsx:164-169`                                              | ✅ | |
| DOB                 | `DOB: 1958-09-03`                                 | `DOB: {dob}` `PatientHeader.tsx:185`                                                     | ✅ | FHIR `birthDate` already YYYY-MM-DD. |
| Age                 | `Age: 67` computed                                | `computeAge()` `PatientHeader.tsx:69-96`, rendered `:189`                                | ✅ | Hand-rolled. |
| Patient avatar      | Placeholder silhouette via `pic_array($pid, ...)` | `<UserIcon>` placeholder `PatientHeader.tsx:148-153`                                     | ✅ | API-map Q3 still outstanding (`Patient.photo` not exercised). Both render a generic silhouette for synthetic data — equivalent. |
| Close ("x") button  | Clears patient context                            | Anchor to `/` `PatientHeader.tsx:173-180`                                                | 🔒 | STUB. Inventory itself flagged target as TODO. |
| Encounter picker    | `Select Encounter (N)` dropdown, N = encounter count | Hardcoded `Select Encounter (1)` `aria-disabled` div `PatientHeader.tsx:206-214`         | 🔒 | STUB per task brief; literal "(1)" hardcoded matching Gloria's screenshot. |
| Open Encounter      | `Open Encounter: None` or encounter date           | Hardcoded `Open Encounter: None` `PatientHeader.tsx:215-220`                             | 🔒 | STUB. |
| Loaded state        | screenshot 10 (Gloria, 1958-09-03, Age 67, "(1)")  | `PatientHeaderView` renders all of the above                                              | ✅ | |
| Empty/error state   | n/a — header always populated                      | `ErrorCard` on fetch failure `PatientHeader.tsx:268-278`                                 | 🟦 | Port-only error path; original has no equivalent because PHP renders inline. |

## Allergies

Source: `AllergiesCard.tsx`. Inventory: `dashboard-inventory.md → ## Allergies → Fields`.
References: `reference-screenshots/11-card-allergies-loaded.png`, `21-card-allergies-empty.png`.

| Field/state           | Original                                                  | Port                                                                              | Status | Notes |
|-----------------------|-----------------------------------------------------------|------------------------------------------------------------------------------------|--------|-------|
| Card title            | `xl('Allergies')`                                         | `CARD_TITLE = "Allergies"` `AllergiesCard.tsx:30,121`                              | ✅ | |
| Allergen name         | `l.title`                                                 | `allergyDisplay(allergy)` `AllergiesCard.tsx:138,148-150`                          | ✅ | API map: `code.text` → `text.div` fallback. |
| Severity (parens)     | `{{ allergen }} ({{ severity }})` per twig template       | `{allergen} ({severity ?? ""})` `AllergiesCard.tsx:148-150`                         | ✅ | Empty parens preserved when severity absent (matches screenshot 11 "Sulfonamide ()"). |
| Severity badge        | n/a                                                       | Adds `<span>` badge with severity text `AllergiesCard.tsx:159-166`                  | ➕ | Port-only: original surfaces severity only inside parens. The bordered chip is additive. |
| Reaction              | Hover tooltip via `title=` only (no inline display)       | `title=` tooltip AND inline muted `<span>` `AllergiesCard.tsx:146,151-158`         | ⚠ | Deviation: port renders reaction inline as visible muted text in addition to the tooltip. |
| Severity-flag highlight | `bg-warning font-weight-bold` when severity ∈ severe/life_threatening_severity/fatal (FHIR `criticality=high`) | Not implemented — no criticality-driven highlight in `AllergiesCard.tsx`            | ⚠ | Deviation/regression: severity-based row highlighting from twig template was not ported. API map calls this out (criticality=high → bg-warning). Clinical-safety-adjacent. |
| Empty states (touched / untouched) | `No Known Allergies` (twig:27-30) / `Nothing Recorded` | Both collapsed to `<EmptyCard message="None" />` `AllergiesCard.tsx:108-114`        | ✅ (with caveat counted as 🔒 in summary) | Brief decision: comment at `:111-113` acknowledges deviation from "No Known Allergies"; original/touched-untouched distinction has no FHIR equivalent. Counted as OOS in summary because brief explicitly overrides. |
| Edit affordance       | Pencil → `load_location(stats_full.php?...&category=allergy)` | Pencil button, `console.log` STUB `AllergiesCard.tsx:122-133`                       | (counted under empty-state OOS row) | OOS per brief. |
| Loaded (screenshot 11) | "Sulfonamide ()" single row                       | `AllergiesCardView` renders allergen + parens                                       | ✅ | |

## Medical Problems

Source: `MedicalProblemsCard.tsx`. Inventory: `dashboard-inventory.md → ## Medical Problems → Fields`.
Reference: `reference-screenshots/12-card-problems-loaded.png`.

| Field/state         | Original                                                | Port                                                                          | Status | Notes |
|---------------------|---------------------------------------------------------|--------------------------------------------------------------------------------|--------|-------|
| Card title          | `xl('Medical Problems')`                                | `CARD_TITLE = "Medical Problems"` `MedicalProblemsCard.tsx:37,183`             | ✅ | |
| Problem title       | `l.title` only — single-line entry per twig:17-21        | `formatProblemDisplay()` `MedicalProblemsCard.tsx:48-58,218-220`               | ✅ | |
| Onset date          | Not rendered on dashboard card (twig shows title only)   | `formatOnsetDate(c.onsetDateTime)` `MedicalProblemsCard.tsx:79-85,221-228`     | ➕ | Port-only per brief; inventory says original renders title only. |
| Status badge        | Not rendered on dashboard card                            | `<StatusBadge>` `MedicalProblemsCard.tsx:143-161,229`                          | ➕ | Port-only per brief. Card preamble at `:14-19` documents this. |
| Active filter       | `filterActiveIssues()` excludes resolved + end-dated     | `filterVisibleProblems()` keeps active/recurrence/relapse/inactive/remission `MedicalProblemsCard.tsx:93-112` | ✅ | FHIR equivalent of carry-forward; broader (also includes `inactive`/`remission`) — documented in code. |
| Empty state         | `None{{Issues}}` (touched) / `Nothing Recorded` (untouched) | `<EmptyCard ... message="None" />` `MedicalProblemsCard.tsx:177`               | 🔒 | Brief decision (same pattern as Allergies). |
| Edit affordance     | Pencil → stats_full.php?...&category=medical_problem    | Pencil button STUB `MedicalProblemsCard.tsx:188-197`                           | 🔒 | OOS per brief. |
| Loaded (screenshot 12) | "Heart failure" single row                            | renders display + (port-only) onset + status                                  | ✅ | |

## Medications

Source: `MedicationsCard.tsx`. Inventory: `dashboard-inventory.md → ## Medications → Fields`.
Reference: `reference-screenshots/13-card-medications-loaded.png`.

| Field/state         | Original                                                | Port                                                                          | Status | Notes |
|---------------------|---------------------------------------------------------|--------------------------------------------------------------------------------|--------|-------|
| Card title          | `xl('Medications')`                                     | `CARD_TITLE = "Medications"` `MedicationsCard.tsx:29,69,101`                   | ✅ | |
| Drug name + dosage  | `m.title` + `m.drug_dosage_instructions` rendered single line | `formatMedicationDisplay()` joins concept + dosage with " — " separator `MedicationsCard.tsx:44-58,120,133` | ⚠ | Deviation: original screenshot 13 shows "Furosemide 80mg IV" as single concatenated string; port may render `{name} — {dosage}` if `dosageInstruction[0].text` is also populated. The em-dash separator departs from the original layout. |
| Status (when not active) | n/a — original lists `lists.title` only           | Parenthetical `(on-hold)` / `(completed)` next to non-active rows `MedicationsCard.tsx:125,134-141` | ➕ | Port-only per synthesis rule. |
| Status filter       | `filterActiveIssues()` excludes resolved/end-dated      | `filterMedications()` keeps status ∈ {active, on-hold, completed} (delegated to `lib/fhir/synthesis`) `MedicationsCard.tsx:177` | ✅ | Carry-forward of inventory rule. |
| Empty state         | `None` / `Nothing Recorded`                             | `<EmptyCard message="None" />` `MedicationsCard.tsx:94-96`                     | 🔒 | Same brief decision. |
| Edit affordance     | Pencil → stats_full.php?...&category=medication         | Pencil STUB `MedicationsCard.tsx:105-116`                                      | 🔒 | OOS per brief. |
| Loaded (screenshot 13) | Two rows: Furosemide 80mg IV, Lisinopril 10mg PO     | `MedicationsCardView` renders both                                            | ✅ | `divide-y` row separator is minor styling — parity. |

## Prescriptions

Source: `PrescriptionsCard.tsx`. Inventory: `dashboard-inventory.md → ## Prescriptions`.
Reference: `reference-screenshots/14-card-prescriptions-empty.png`.

| Field/state          | Original                                              | Port                                                                          | Status | Notes |
|----------------------|-------------------------------------------------------|--------------------------------------------------------------------------------|--------|-------|
| Card title           | `xl('Prescriptions')`                                 | `CARD_TITLE = "Prescriptions"` `PrescriptionsCard.tsx:30,85,111`               | ✅ | |
| Drug name            | `prescriptions.drug`                                  | `formatMedicationName()` `PrescriptionsCard.tsx:45-60,138-140`                 | 🟦 | Loaded shape inferred — no synthetic prescription. |
| Dosage / form / unit / route / interval | Smarty fragment renders via `generate_display_field()` | `formatDosage()` flattens to `dosageInstruction[0].text` `PrescriptionsCard.tsx:67-70,141-148` | 🟦 | API-map Q5 still open: structured `doseAndRate`/`timing`/`route` not verified. |
| Prescriber           | `prescriptions.requester` (eRx variant)               | `formatPrescriber()` → `requester.display` `PrescriptionsCard.tsx:72-75,149-156` | 🟦 | Loaded shape inferred — no synthetic data. |
| Empty state          | "None" (matches every synthetic patient)              | `<EmptyCard message="None" />` `PrescriptionsCard.tsx:104-106`                  | ✅ | Direct parity with screenshot 14. |
| Edit affordance      | Pencil → iframe modal `controller.php?prescription&list&id={pid}` | Pencil STUB `PrescriptionsCard.tsx:112-124`                                    | 🔒 | OOS per brief. |
| eRx variant title / "Add" button | `'Prescription History'` / Add button when eRx enabled | Not implemented                                                       | 🔒 | Inventory: eRx variant is OOS for synthetic dataset. |

## Care Team

Source: `CareTeamCard.tsx`. Inventory: `dashboard-inventory.md → ## Care Team → Fields`.
Reference: `reference-screenshots/15-card-careteam-empty.png` (loaded screenshot does not exist — synthetic gap).

| Field/state         | Original column                                          | Port                                                                          | Status | Notes |
|---------------------|----------------------------------------------------------|--------------------------------------------------------------------------------|--------|-------|
| Card title          | n/a (no card title — manage_care_team.html.twig)         | `CARD_TITLE = "Care Team"` `CareTeamCard.tsx:32,129`                           | ✅ | Title chrome added for consistency with peer cards. |
| Team Name (`<h5>`)  | `team_name` rendered above table in view mode (`manage_care_team.html.twig:175-180`) | Not rendered                                                                   | ➖ | Missing: port renders only participant rows, no team-level name header. |
| Team Status badge   | `team_status_display` badge next to name in view mode    | Not rendered                                                                   | ➖ | Missing. |
| Type column (Provider / Related Person) | `member.member_type` shown as badge          | Not rendered                                                                  | ➖ | Missing. API map: RelatedPerson not in US Core 3.1.1; Type chip dropped. |
| Member              | `member.user_display` / `member.contact_name`            | `memberDisplay(participant)` `CareTeamCard.tsx:58-63,151,163-165`               | 🟦 | Loaded shape inferred only. |
| Role                | `member.role_display` (resolved from list_options)        | `roleText(participant)` `CareTeamCard.tsx:69-87,153,166-173`                    | 🟦 | Inferred only. |
| Facility            | `member.facility_display`                                 | `participant.onBehalfOf?.display` `CareTeamCard.tsx:154,174-181`                | 🟦 | API-map Q4 outstanding; inferred. |
| Since               | `member.provider_since` (date)                            | Not rendered                                                                  | ➖ | Missing — `participant.period.start` not surfaced. |
| Status (per-row)    | `member.status_display` (per-participant)                 | Not rendered (FHIR has no per-participant status field)                        | 🔒 | API map: "FHIR has no per-participant status" — accept as documented limitation. |
| Note                | `member.note`                                             | Not rendered (no FHIR mapping)                                                 | ⚠ | API map: "drop the column from the port, or extend with a non-FHIR call" — port chose drop. Acknowledged in `CareTeamCard.tsx:144-149`. Defend in migration doc. |
| Empty state         | Empty table header rendered with no rows (screenshot 15) | `<EmptyCard message="None" />` `CareTeamCard.tsx:117-124`                       | ✅ | Code comment at `:118-123` acknowledges this is a brief simplification. |
| Edit affordance     | Pencil → in-place edit mode (`btn-edit-care-team`); label "Add"/"Edit" depending on state | Pencil STUB `CareTeamCard.tsx:130-141`                                          | 🔒 | OOS per brief. |
| Loaded state        | No screenshot exists                                      | View renders inferred row shape                                                | 🟦 | |

## Vitals

Source: `VitalsCard.tsx`. Inventory: `dashboard-inventory.md → ## Vitals → Fields`.
References: `reference-screenshots/16-card-vitals-loaded.png`, `24-card-vitals-loaded-alejandro.png`.

| Field/state         | Original                                                       | Port                                                                          | Status | Notes |
|---------------------|----------------------------------------------------------------|--------------------------------------------------------------------------------|--------|-------|
| Card title          | "Vitals" (loader-injected card)                                | `CARD_TITLE = "Vitals"` `VitalsCard.tsx:46,231,268`                            | ✅ | |
| "Most recent vitals from `<date>`" header | `form_vitals.date` rendered as a sentence (screenshot 16) | Not rendered as a sentence; only `Last updated: ...` `VitalsCard.tsx:286-292` | ⚠ | Deviation: original surfaces both "Most recent vitals from {date}" AND "Last Updated {datetime}"; port collapses to a single "Last updated" derived from the latest `effectiveDateTime` (`lastUpdatedLabel()` `:199-221`). |
| Blood Pressure      | `bps/bpd` (e.g. "156/94")                                       | BP-panel synthesis from `component[]` `VitalsCard.tsx:140-148`                 | ✅ | LOINC 85354-9 panel handled. |
| Temperature (F + °C in parens) | F shown with C in parens (screenshot 16: "37 F (2.78 C)") | Single `valueQuantity` line via `formatObservationValue()` `:150-153`           | ⚠ | Deviation: port emits `{value} {unit}` only — the C-in-parens conversion is not implemented. API-map Q (Temperature unit) flagged this. |
| Temp Method         | `form_vitals.temp_method` rendered as own row (e.g. "Oral")    | Not rendered (no LOINC in vitals service — API-map Q6)                         | ⚠ | Deviation: original card row missing. API map: "NOT FOUND in LOINC table"; port silently drops. |
| Pulse               | `form_vitals.pulse` "per min"                                   | LOINC 8867-4 → `valueQuantity.value` + unit `VitalsCard.tsx:150-153`            | ✅ | |
| Respiration         | `form_vitals.respiration` "per min"                             | LOINC 9279-1 → `valueQuantity.value` + unit                                    | ✅ | |
| Oxygen Saturation   | `form_vitals.oxygen_saturation` "%"                             | LOINC 2708-6 / 59408-5 → `valueQuantity.value` + unit                          | ✅ | |
| Last Updated        | `form_vitals.last_updated` standalone row                       | `lastUpdatedLabel()` aggregated as `Last updated: ...` `VitalsCard.tsx:286-292` | ✅ | Date format converted to `YYYY-MM-DD HH:mm`. |
| Row order           | Fixed: BP, Temp, Temp Method, Pulse, Respiration, O2, Last Updated (screenshot 16) | Alphabetical via `displayLabel.localeCompare` `VitalsCard.tsx:261`              | ⚠ | Deviation: alphabetical. Code comment at `:256-260` documents departure ("the inventory's seven-row hardcoded sequence is form-specific, not LOINC-driven"). |
| Card vs form fields  | Card filters to 7 specific rows; form_vitals also has Weight/Height/BMI/Head/Waist not shown on dashboard | `mostRecentByLoinc` returns ALL grouped vital-signs LOINCs from the bundle      | (counted with row order) | API map §"Card display vs underlying form" recommends filtering to seven LOINCs; port skips that filter. |
| Empty state         | `No vitals have been documented.`                                | `<EmptyCard message="No vitals recorded" />` `VitalsCard.tsx:252-254`           | ✅ | Equivalent text. |
| Edit / "Trend" link | "Trend" button → `trend_form.php?formname=vitals&context=dashboard` | Pencil STUB `VitalsCard.tsx:269-283`                                            | 🔒 | OOS per brief. Code comment at `:273-277` documents that original uses "Trend" not pencil — chrome consistency override. |
| Loaded — Alejandro  | Screenshot 24 shows expanded form, not the dashboard card         | n/a — port renders the same Observation set                                    | 🟦 | Loaded-card-shape verification limited to screenshot 16 (Gloria). |

## Triage

### Fix before submit

1. **Allergies — severity highlight (`bg-warning`) is missing.** Original
   highlights severe / life-threatening / fatal allergy rows in bold/yellow.
   API map names `criticality=high` as the FHIR equivalent.
   `AllergiesCard.tsx` doesn't read `criticality` at all. This is a
   clinical-safety-adjacent regression — implement before shipping.
2. **Vitals — Temperature dual-unit display (F + °C in parens).** Screenshot
   16 shows "37 F (2.78 C)". Port emits `{value} {unit}` only. Either
   implement the dual-unit format or document the simplification.
3. **Care Team — Team Name and Team Status header are not rendered.**
   Original `manage_care_team.html.twig:175-180` puts the team name in an
   `<h5>` and status next to it in view mode. Port renders only participant
   rows. Surface `careTeams[0].name` / `.status` above the participant list
   when present (loaded shape inferred — synthetic data is empty).

### Defend in migration doc

- **Empty-state collapse** (`No Known Allergies` / `Nothing Recorded` /
  `None{{Issues}}` → unified `"None"`) for Allergies, Medical Problems,
  Medications. Brief decision; document the touched/untouched gap (no FHIR
  equivalent).
- **Medical Problems port-only fields** (onset date + status badge). Brief
  expanded the card beyond the original twig's title-only display.
- **Medications status parenthetical** for non-active rows (port-only).
- **Vitals row order alphabetical**, not BP→Temp→…→Last Updated. Code at
  `VitalsCard.tsx:256-260` already comments on this; promote to migration
  doc.
- **Vitals does not filter to the seven LOINCs the original card pinned.**
  Surfaces all grouped vital-signs LOINCs.
- **Patient name rendered as `family, given` `<span>` instead of original's
  given-first link.** `PatientHeader.tsx:48-49` chose last-name-first;
  screenshot 10 is "Gloria Tran" first-last and is also a hyperlink.
- **Allergies: reaction shown inline + tooltip** (vs. tooltip-only in
  original).
- **Care Team — Note column dropped** (no FHIR mapping; per API map
  recommendation).
- **Vitals — pencil affordance instead of original "Trend" link.**

### Accept as documented limitation

- **Patient.photo / avatar:** API-map Q3 outstanding. Port renders generic
  silhouette; original also renders silhouette for synthetic patients.
- **Encounter picker, Open Encounter, New encounter "+":** STUBs; brief
  scopes them out of the read-only port slice.
- **Care Team — per-participant Status (no FHIR field), Note (no FHIR
  field), Provider/RelatedPerson Type chip (RelatedPerson not in US Core
  3.1.1), `Since` (`participant.period.start` mapping unverified — API-map
  Q4).** All flagged in API map.
- **Prescriptions — every loaded-state field is inferred from FHIR shape,
  not observed.** Synthetic dataset has zero `intent=order` rows across the
  six verified pids (`dashboard-api-map.md` §1.5). Empty-state parity is
  exact (screenshot 14 → `EmptyCard "None"`); loaded parity unverifiable
  until a real prescription exists.
- **Vitals — `Temp Method` and `Waist Circumference` columns dropped.**
  Neither has a LOINC in `FhirObservationVitalsService` (API-map Q6).
