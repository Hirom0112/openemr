# Parity Investigation — 2026-05-08

Read-only investigation of the parity gaps the owner observed (Vitals dumping
all observations, Care Team headers collapsed, layout drift). Focus: root
cause, not symptom.

## TL;DR

1. **Vitals — spec is wrong.** The original card is *not* a curated 7-LOINC
   list; it is a "render every non-empty `form_vitals.*` column" iteration
   in `interface/forms/vitals/report.php:46-49`. The 7 rows on the Gloria
   screenshot are a property of which columns happen to be populated, not a
   curation list. Both the inventory and api-map invented a 7-LOINC curation
   rule that does not exist in the source. The port followed the api-map and
   filtered nothing, so it dumps all LOINCs (panel `85353-1` excluded).
2. **Care Team — implementation deviates from spec, and spec was already
   wrong about it.** Original `manage_care_team.html.twig:189-207` *always*
   renders the table-with-headers (Type, Member, Role, Facility, Since,
   Status, Note, Remove); empty body = empty `<tbody>`. The port short-
   circuits to `<EmptyCard message="None" />` (`CareTeamCard.tsx:117-124`),
   the inventory documents this as "brief simplification," and parity-audit
   marked it ✅ — three places where parity got accepted away from the
   visible reference.
3. **Layout — inventory got the column placement wrong for Vitals, and the
   port grid does not match the original three-row structure.** Vitals is
   in the LEFT column (`col-md-8`, `demographics.php:1327→1572`), not the
   right column as inventory says (`dashboard-inventory.md` Vitals → Position
   says `col-md-4 demographics.php:1573` — that line opens the right column
   that holds Portal/Reminders/etc.).
4. **Process root cause — the parallel-launch in 4.2–4.8 violated the plan.**
   `execution-plan.md:230-265` lists 4.2 through 4.7 as separate ~30 min
   slots each ending with "side-by-side visual comparison vs reference
   screenshot." `git log` shows commit `65618004a` shipped 4.2–4.8 as one
   parallel batch with the side-by-side step marked PENDING for every card
   (`execution-plan.md:225, 234, 254` etc.). The gates the plan required
   between cards were skipped.
5. **Auth-code flow has never been clicked in a browser.**
   `auth-notes.md:75-81` and `execution-plan.md:186-188` admit it; 1.5
   verified password-grant only. The cards landed on top of an
   architecturally-unverified auth path.

## Sources read

- `patient-dashboard/CLAUDE.md` — project rules; reimplementation not redesign,
  inventory + api-map are the spec.
- `patient-dashboard/dashboard-inventory.md` — every section. Spec for fields
  per card; flags Vitals position as `col-md-4` (incorrect — see B3).
- `patient-dashboard/dashboard-api-map.md` — every endpoint. Names a 7-LOINC
  vital-signs curation rule that does not exist in the original PHP source.
- `patient-dashboard/PATIENT_DASHBOARD_MIGRATION.md` — defense doc, owned by
  the human; informs scope and "what we changed" framing.
- `patient-dashboard/auth-notes.md` — auth-code path untested in browser.
- `patient-dashboard/parity-audit.md` (the "morning audit" referenced as
  PARITY-AUDIT-MORNING.md) — already flags Vitals row order, Care Team
  headers, severity highlight, etc. as deviations.
- `patient-dashboard/execution-plan.md` — every phase; 4.2–4.7 specified as
  sequential per-card cycles ending in side-by-side comparison.
- `reference-screenshots/00-dashboard-FULL-PAGE-gloria.png.png` — full page;
  Vitals appears in lower-left of left column, NOT in the right column.
  Right column holds Portal / Clinical Reminders / Recall / Appointments /
  Health Concerns / Immunizations.
- `reference-screenshots/15-card-careteam-empty.png` — empty Care Team
  shows full table headers (Type, Member, Role, Facility, Since, Status,
  Note, Remove) with empty body row.
- `reference-screenshots/16-card-vitals-loaded.png` — 7 rows: BP, Temp,
  Temp Method, Pulse, Resp, O2, Last Updated. Order is fixed.
- `reference-screenshots/24-card-vitals-loaded-alejandro.png` — *form view*,
  not the dashboard card; provided as evidence the form has more fields.
- `interface/patient_file/summary/demographics.php:1085-1573` — three rows:
  (1) inline Twig 4-up cards, (2) Care Team + preference cards as col-12,
  (3) col-md-8 left (Demographics, Insurance, Labs, **Vitals**, ...) /
  col-md-4 right (Portal, Reminders, ...).
- `interface/patient_file/summary/vitals_fragment.php:26-47` — most-recent
  `form_vitals` row by `pid` ordered by `date DESC`, then calls
  `vitals_report()`.
- `interface/forms/vitals/report.php:34-180` — `vitals_report()`. Iterates
  every column on the row, skips empties / structural columns
  (lines 46-53), formats per-key (BP merged from `bps`+`bpd`, weight,
  temperature dual-unit, etc.). **Not a curated LOINC list.**
- `templates/patient/card/manage_care_team.html.twig:189-207` — table
  always rendered with full thead; tbody empty when no participants.
- `templates/patient/card/allergies.html.twig:38-49` — severity highlight
  via `bg-warning font-weight-bold` for severe / life_threatening / fatal.
- `templates/patient/card/medical_problems.html.twig` — title only.
- `templates/patient/card/medication.html.twig` — title + dosage.
- `src/Patient/Cards/CareTeamViewCard.php` — hydrates `existing_care_team`
  array consumed by JS; team-level name + status badge in view mode.
- `src/Services/FHIR/Observation/FhirObservationVitalsService.php:87-264` —
  LOINC table is comprehensive (BP panel, BP sys/dia, pulse, resp, temp,
  O2 sat, weight, height, BMI, head circ, peds variants). It is NOT a
  7-LOINC card-display subset.
- `web/src/components/cards/VitalsCard.tsx` — fetches Observation?category=
  vital-signs, groups by LOINC, picks most-recent per LOINC, sorts
  alphabetically. **No 7-LOINC filter, no fixed row order, no Temp Method,
  no "Most recent vitals from" header line.**
- `web/src/components/cards/CareTeamCard.tsx:117-124` — empty short-circuit
  to `<EmptyCard message="None" />` instead of an empty headers table.
- `web/src/app/patient/[id]/page.tsx:64-118` — grid: 3-col outer, with
  left 2-col holding a 2x2 of Allergies/MedProblems/Medications/
  Prescriptions and right 1-col holding Vitals; Care Team full-width
  beneath. Does NOT match the original three-row structure.
- `git log -- patient-dashboard/web/` — commit `65618004a` ships
  Phase 4.2–4.8 (six cards) in one batch.

## Phase A answers — per card

### Patient header

- (a) Inventory: name (link), MRN/pubpid, close x, avatar, DOB, age,
  encounter picker, open encounter, new encounter (`dashboard-inventory.md`
  → `## Patient header` → `### Fields`).
- (b) Reference screenshot 00: name is a blue hyperlink "Gloria Tran" first-
  last; "(4)" follows, then DOB, Age, "Select Encounter (1)", "Open
  Encounter: None", "+", "x".
- (c) Built by `OemrUI` chrome at `demographics.php:363-375` with
  `include_patient_name=true`; not a Twig template the inventory traced.
- (d) `GET /Patient/{uuid}` (`dashboard-api-map.md` → `## Patient header`).
- (e) `web/src/components/cards/PatientHeader.tsx:41-220`. Renders `family,
  given` last-first as a `<span>`, not first-last as a link
  (`parity-audit.md:41` already flags this).

### Allergies

- (a) Allergen + (severity) with reaction in tooltip; row highlighted
  `bg-warning font-weight-bold` when severity ∈ severe / life_threatening /
  fatal (`dashboard-inventory.md` → `## Allergies` → `### Fields`,
  `templates/patient/card/allergies.html.twig:38-49`).
- (b) Screenshot 11 shows "Sulfonamide ()" with empty parens — Gloria has
  no severity. No highlight visible because severity is null.
- (c) `demographics.php:1112-1133` + `templates/patient/card/allergies.html.twig`.
- (d) `GET /AllergyIntolerance?patient={uuid}`; severity highlight keys off
  `criticality=high` per api-map.
- (e) `AllergiesCard.tsx`. Fixed in `d0d6997e1` for severity highlight per
  `parity-audit.md` "Fix before submit" #1. Reaction rendered inline +
  tooltip (deviation per audit).

### Medical Problems

- (a) Title only (`dashboard-inventory.md` → `## Medical Problems`,
  `medical_problems.html.twig:17-21`).
- (b) Screenshot 12 shows "Heart failure" — single line, no badge, no date.
- (c) `demographics.php:1135-1157`.
- (d) `GET /Condition?patient={uuid}&category=problem-list-item`.
- (e) `MedicalProblemsCard.tsx`. Adds onset date + status badge as port-only
  (`parity-audit.md:78-79`).

### Medications

- (a) `lists.title` + `lists.drug_dosage_instructions` on a single line.
- (b) Screenshot 13: "Furosemide 80mg IV" / "Lisinopril 10mg PO".
- (c) `demographics.php:1159-1179` + `templates/patient/card/medication.html.twig`.
- (d) `GET /MedicationRequest?patient={uuid}`, status filter client-side.
- (e) `MedicationsCard.tsx`. Joins concept + dosage with " — " separator
  (deviation, `parity-audit.md:93`).

### Prescriptions

- (a) Smarty fragment, drug name + dosage/form/unit/route/interval.
- (b) Screenshot 14: "None" — empty, all synthetic patients.
- (c) `demographics.php:1181-1243`.
- (d) `GET /MedicationRequest?...&intent=order&status=active`.
- (e) `PrescriptionsCard.tsx`. Empty for every synthetic patient (parity).

### Care Team

- (a) Inventory: full table — Type, Member, Role, Facility, Since, Status,
  Note, Remove + team-name + team-status badge above
  (`dashboard-inventory.md` → `## Care Team` → `### Fields`).
- (b) **Screenshot 15**: empty body, but ALL eight column headers visible
  in a styled thead row. There is no "None" or empty-card placeholder —
  the empty state IS the headers-with-empty-body.
- (c) `demographics.php:1247-1272` + `manage_care_team.html.twig:165-207`.
  `<div class="col-12 m-0 p-0 px-2">` wraps the rendered table.
- (d) `GET /CareTeam?patient={uuid}&status=active`.
- (e) `CareTeamCard.tsx:117-124` — short-circuit to
  `<EmptyCard message="None" />` when participants array is empty. Team-
  level name + status header restored in `d0d6997e1`. **Empty-state shape
  is wrong vs reference**.

### Vitals

- (a) Inventory claims "Visible labelled fields per the screenshot":
  BP, Temperature, Temp Method, Pulse, Respiration, Oxygen Saturation,
  Last Updated (`dashboard-inventory.md` → `## Vitals` → `### Fields`).
- (b) Screenshot 16 (Gloria) shows exactly those 7 rows in that order,
  preceded by "Most recent vitals from: 2026-04-29 05:45:00", followed by
  "Click here to view and graph all vitals." link.
- (c) **Source is `interface/forms/vitals/report.php:46-49`**:
  ```php
  foreach ($data as $key => $value) {
      if (in_array($key, ["uuid","id","pid","user","groupname","authorized","activity","date"])
          || Utilities::isDateEmpty($value) || $value == "0.0") { continue; }
      ...
  }
  ```
  This is a **render-every-non-empty-column** loop, not a curation list.
  Gloria shows 7 rows because exactly those 7 columns are populated for
  her latest `form_vitals` row. A different patient with `weight`,
  `height`, `BMI` populated would render those rows on the same card.
  Position: left column `col-md-8`, `demographics.php:1503-1526`
  (NOT `col-md-4` as inventory says).
- (d) `GET /Observation?patient={uuid}&category=vital-signs`. The api-map
  recommends "filter to the seven displayed LOINCs client-side"
  (`dashboard-api-map.md` → "Card display vs underlying form") — that
  recommendation has no basis in the original PHP source.
- (e) `VitalsCard.tsx:285-345`. No 7-LOINC filter, sorts alphabetically,
  drops the "Most recent vitals from" header line, drops Temp Method (no
  LOINC), no "Click here" link.

## Phase B findings

### B1 — Vitals curation root cause

**Hypothesis confirmed: wrong source / spec invented a curation rule.**

The original card is server-rendered from `form_vitals` (a single MySQL
table, one row per encounter), not from the FHIR Observation stream. The
function `vitals_report()` (`interface/forms/vitals/report.php:34-180`):

1. Fetches `formFetch("form_vitals", $id)` — one row, all columns.
2. Iterates the row's columns.
3. Skips columns that are empty / "0.0" / structural metadata (lines 46-53).
4. Renders the rest with per-key formatting (BP combines `bps`+`bpd`;
   Weight, Height, Temperature have unit-conversion logic; Pulse /
   Respiration / O2 / BMI / Oxygen Flow share a numeric branch).

The 7-row Gloria screenshot is therefore a **data-shape consequence**, not
a curated subset. There is **no list of 7 LOINCs to filter against** in the
original codebase. The api-map's "filter to the seven displayed LOINCs
client-side" recommendation invented that rule.

`FhirObservationVitalsService.php:87-264` exposes the full LOINC catalogue
(BP panel + components, pulse, resp, temp, O2 sat, weight, height, BMI,
head circ, peds variants). This is what the port queries; nothing in this
service curates to "what dashboard shows."

The port's behaviour is correct given the api-map ("surface all grouped
vital-signs LOINCs") but wrong vs the original ("render whatever
non-empty fields the latest form_vitals row carries"). The fix is at the
**spec layer first**: rewrite `dashboard-inventory.md` → Vitals → Fields
and `dashboard-api-map.md` → Vitals → "Card display vs underlying form" to
state the actual rule (non-empty form_vitals columns → equivalent LOINCs
client-side, with fixed display order matching `vitals_report()`'s
case-by-case ordering: BP first via panel, then alphabetical/Twig key
order). Only then change `VitalsCard.tsx` to:
  - render the "Most recent vitals from: {effectiveDateTime}" header line
  - render Temp Method (FHIR `Observation.method.text` per api-map Q6 —
    open question still)
  - emit fixed row order BP → Temp → Temp Method → Pulse → Resp → O2 →
    Last Updated when those rows are present
  - render the trailing "Click here to view and graph all vitals." link
  - use a `<dl>` two-column label/value grid as it already does

Classification: **(b) Spec was incomplete + (a) port followed the wrong
spec. Fix at spec first.**

### B2 — Care Team empty state root cause

**Hypothesis confirmed: explicit "parity simplification" that contradicts
the reference.**

Original (`manage_care_team.html.twig:189-207`): the `<table class="table
table-sm table-striped">` with `<thead class="thead-light">` containing 8
`<th>` cells (Type, Member, Role, Facility, Since, Status, Note, Remove)
is **always rendered**. The table body is JS-populated (line 204:
`{# Populated by JS #}`); when there are no participants, the body is
empty but the headers and the styled thead row remain visible
(reference-screenshots/15-card-careteam-empty.png shows exactly that —
the grey-tinted header row with eight columns and a blank body row below).

Port (`CareTeamCard.tsx:117-124`):
```tsx
if (participants.length === 0) {
    return <EmptyCard title={CARD_TITLE} message="None" />;
}
```
The participants-empty branch returns the standard "None" empty card —
the same primitive used by Allergies, Medical Problems, Medications,
Prescriptions when those lists are empty. The eight column headers never
render in the empty state.

This is documented in three places as a deliberate choice:
- code comment `CareTeamCard.tsx:118-123` ("Parity simplification: ...
  collapses that to the standard EmptyCard 'None' treatment so all empty
  cards on the dashboard speak the same visual language")
- inventory `dashboard-inventory.md` → Care Team → States — describes the
  empty as "table header rendered with no rows below it. No 'Nothing
  Recorded' placeholder" — and the port still chose to add one
- `parity-audit.md` Care Team row "Empty state": ✅ "Code comment ...
  acknowledges this is a brief simplification."

The owner's complaint is correct: this is a feature-parity regression
hiding behind a "we made it consistent" justification. The spec
(inventory) accurately describes the original, the port deliberately
deviates from it, and the audit accepted the deviation.

Classification: **(a) Implementation deviates from spec.** The simpler
fix-path: keep `CareTeamCard.tsx` rendering the table and headers
unconditionally; render an empty `<tbody>` (or a single empty row) when
participants.length === 0. The "all empty cards speak the same visual
language" reasoning is a redesign argument, which `CLAUDE.md` non-
negotiable #1 forbids ("reimplementation, not redesign … visual departure
from the original requires explicit justification in the migration doc").

### B3 — Layout root cause

**Inventory misread the column placement of Vitals; port grid does not
mirror the original three-row structure.**

Original (`demographics.php:1085-1326`):
- Row 1 (`<div class="row">` line 1088): four inline-Twig cards as
  `<div class="$col">` where `$col` = `col-md-{12 / cards}`. With
  Allergies + MedProblems + Meds enabled, `$col = "col-md-4"`. Four cards
  side-by-side spanning the full row width. (The inventory's
  `col-md-8` claim for Allergies, MedProblems, Medications is wrong —
  those are `col-md-4` *inside* the first row, not inside a `col-md-8`
  parent.)
- Row 2 (`<div class="row">` line 1246): full-width `col-12` blocks —
  Care Team (line 1268), Treatment Preferences (1291), Care Experience
  (1319). All three render at full width below row 1.
- Row 3 (`<div class="row">` opens via the col-md-8 / col-md-4 split):
  `col-md-8` left at line 1327 (Demographics, Billing, Insurance, Labs,
  **Vitals**, LBF charted forms), `col-md-4` right at line 1573 (Portal,
  Clinical Reminders, Recall, Appointments, Health Concerns,
  Immunizations).

So **Vitals is in the LEFT column** of row 3, not the right column.
`dashboard-inventory.md` → `## Vitals` → `### Position` says
"Right column (`col-md-4`, `demographics.php:1573`)" — that line opens
the right column, and Vitals lives at `:1503-1526` which is BEFORE that
closing `</div>` of the LEFT column at `:1572`. The inventory misread
the column.

Port (`web/src/app/patient/[id]/page.tsx:78-108`):
- Outer `grid grid-cols-1 md:grid-cols-3` (3-col).
- Inner left `md:col-span-2 md:grid-cols-2` — 2x2 grid of Allergies,
  Medical Problems, Medications, Prescriptions.
- Inner right `md:col-span-1` — Vitals.
- Care Team full-width below.

Mismatches:
1. Allergies/MP/Meds/Rx are 2x2 in the port, 4-up across in the original.
2. Vitals lives next to the clinical cards in the port (right rail of
   row 1); in the original it's in row 3 / left column far below the
   clinical cards, beneath Demographics/Insurance/Labs (out-of-scope
   sections).
3. Care Team is full-width below row 1 in the port; in the original it's
   in row 2 (full width also, but separated from the clinical cards by
   the row break).
4. The original's row 3 structure (col-md-8 + col-md-4) doesn't exist in
   the port at all — that whole row is out-of-scope sections in the
   original, but Vitals lives inside it on the left, so the port had to
   either reproduce the row 3 left column or place Vitals elsewhere.

Classification: **(b) Spec was incomplete (no per-card "Position" entry
gives the row index — only the col class) + (c) inventory was wrong
about Vitals placement. Fix spec first; then redo grid.** A faithful port
needs a top-level page grid that matches:
  - row 1: 4-up of Allergies / MedProblems / Medications / Prescriptions
  - row 2: full-width Care Team
  - row 3: a single column on the left where Vitals sits (the
    out-of-scope row-3 right column doesn't need to be reproduced; the
    out-of-scope row-3 left siblings — Demographics, Labs, etc. — also
    don't need to be reproduced).

### B4 — Process root cause / post-mortem

**The cards landed without the per-card verification gate the plan
required.**

`execution-plan.md:214-276` (Phase 4) specifies seven sub-phases (4.1
through 4.7) each ~30 min ending with "side-by-side visual comparison vs
reference screenshot," plus 4.8 for the edit-pencil stubs.
`CLAUDE.md` non-negotiable #4: "Slice your work. One file or one bounded
change per response… Do not queue parallel work until the pattern has
been verified once."

What actually happened (`git log -- patient-dashboard/web/`):
- `2c6e2d084` — Phase 4.1 Patient header (pattern-setter), shipped alone.
- `65618004a` — **"Phase 4.2-4.8 — six cards in parallel"**, single
  commit batching all six remaining cards plus the edit-pencil stubs.

Inside `execution-plan.md` the side-by-side comparison checkbox is
unchecked for every card 4.1–4.7 (lines 225, 234, 242, 248, 254, 258,
264). Phase 4.1 was meant to set the pattern *and verify it visually*
before 4.2 began; the visual check on 4.1 itself is also still pending.

Specific misses produced by parallel-launch:
- Sub-agents had only the inventory and api-map to ground them. Both
  documents had the wrong rule for Vitals (B1). No sub-agent re-read
  `vitals_fragment.php` / `forms/vitals/report.php` to validate the
  spec's curation claim against PHP source.
- The "all empty cards should speak the same visual language" reasoning
  in `CareTeamCard.tsx:118-123` is the kind of cross-card consistency
  argument that surfaces when N cards are designed simultaneously by the
  same agent. Sequenced cycles, with a human side-by-side after each,
  would have caught the headers regression on Care Team specifically
  (the screenshot shows headers; the implementation drops them).
- Layout: there is no per-card "Row" entry in inventory, only Position +
  column class. With cards built in parallel the page-level grid was
  composed afterwards from the per-card fragments rather than from the
  original three-row demographics.php structure.

Other process gaps (the owner asked for honesty):
- **Phase 2 vs Phase 3 ordering is fine** (defense doc was drafted
  before scaffolding) but the polished defense in Phase 2.3 was edited
  again *after* Phase 4 (`5.4` cold-read fixes, `d4fca54f3`). That's
  acceptable retrofit, not drift.
- **Phase 3.2 auth-code path was never browser-clicked**
  (`auth-notes.md:75-81`, `execution-plan.md:186-188`). Phase 4 cards
  were built on top of a structurally-untested auth path. If the auth-
  code flow breaks at deploy, the cards will too — and the parity
  investigation cannot exercise them locally either, because the local
  verification path remained on password-grant.
- **No features outside the plan were added** based on the file scan;
  the divergence is structural (layout grid) and behavioural (Care Team
  empty, Vitals filter), not "extra cards."

Verdict: parallel-launch traded depth for speed and skipped the gate the
plan required. The "verify before next card" discipline existed on paper
and was bypassed — knowingly, since `65618004a`'s commit message names
the parallelism. That is the load-bearing process root cause.

### B5 — Spec-vs-implementation classification

| Gap | Classification | Source-of-truth file | Notes |
|-----|----------------|----------------------|-------|
| Vitals dumps every LOINC | (b) Spec was incomplete; (a) port followed wrong spec | `interface/forms/vitals/report.php:34-180` (data) + `vitals_fragment.php:35-46` (chrome) | Spec must move from "7 LOINCs" to "non-empty `form_vitals` columns mapped to LOINCs, fixed display order." Update inventory + api-map first. |
| Vitals row order alphabetical | (a) Implementation deviates from spec | `forms/vitals/report.php:79-180` | Original order is BP, Temp, Temp Method, Pulse, Resp, O2, Last Updated (case branches). Hardcode this order in `VitalsCard.tsx:299`. |
| Vitals "Most recent vitals from" header missing | (a) Implementation deviates from spec | `vitals_fragment.php:36-38` | Render a `<b>` line "Most recent vitals from: {effectiveDateTime}" above the dl. |
| Vitals Temp Method missing | (d) Original is ambiguous (FHIR has no LOINC) | `dashboard-api-map.md` → Q6 | Either render via `Observation.method.text` if populated, or document as a deliberate drop in migration doc. |
| Vitals "Click here…" link missing | (a) Implementation deviates from spec | `vitals_fragment.php:45` | Add a trailing anchor at bottom of card body. |
| Care Team empty state collapsed to "None" | (a) Implementation deviates from spec | `templates/patient/card/manage_care_team.html.twig:189-207` | Render the table + thead unconditionally; tbody empty when participants.length === 0. Remove the EmptyCard short-circuit. |
| Care Team Note column dropped | (d) Original ambiguous (no FHIR mapping) — accept | `manage_care_team.html.twig:199` | Already documented as accepted limitation. Keep dropped, but the `<th>` "Note" should still appear in the headers row to match screenshot 15. |
| Layout 4-up vs 2x2 for clinical cards | (b) Spec was incomplete | `demographics.php:1088-1099` (`$col = col-md-{12/cards}`) | Add per-card "Row" + "Within row" to inventory. Update page grid to 4-up. |
| Vitals column placement | (c) Spec was wrong | `demographics.php:1503-1526` (left col) vs `:1573` (right col opens) | Fix `dashboard-inventory.md` → Vitals → Position to say `col-md-8` left column, row 3. |
| Care Team row vs Vitals row | (b) Spec was incomplete | `demographics.php:1246` (Care Team's row) vs `1327`/`1573` (row 3) | Add row index to inventory. Care Team in row 2; Vitals in row 3 left. |
| Patient name as `family, given` span vs original `Given Family` link | (a) Implementation deviates | `parity-audit.md:41` | Already audited; not fixed. |
| Allergies reaction inline (not tooltip-only) | (a) Implementation deviates | `parity-audit.md:63` | Already audited; not fixed. |
| Medications " — " separator | (a) Implementation deviates | `parity-audit.md:93` | Already audited; defended in migration doc as deliberate. Owner choice. |
| Medical Problems onset + status badge port-only | (a) Implementation deviates | `parity-audit.md:78-79` | Defended in migration doc. Owner choice. |

## Recommended fix sequence

Do not write fix code yet. Do this in order:

**1. Spec fixes (read-only-now, write-after-approval):**
   1. `dashboard-inventory.md` → `## Vitals` → rewrite `### Fields` to
      reflect "render every non-empty `form_vitals` column" rule, drop
      the implication of a 7-LOINC curation. Cite
      `interface/forms/vitals/report.php:46-53`.
   2. `dashboard-inventory.md` → `## Vitals` → fix `### Position`:
      "Left column `col-md-8`, row 3, `demographics.php:1503-1526`."
      Drop the `col-md-4` claim.
   3. `dashboard-inventory.md` → all clinical cards → add a `### Row`
      subsection to disambiguate row 1 (4-up clinical), row 2 (Care Team
      + preferences full-width), row 3 (col-md-8 left holding Vitals).
   4. `dashboard-inventory.md` → `## Care Team` → `### States` → make
      explicit: empty state is "table headers rendered with empty body,"
      NOT "None." Drop the current TODO that hedges.
   5. `dashboard-api-map.md` → `## Vitals` → drop the
      "filter to the seven displayed LOINCs client-side" recommendation.
      Replace with: "Group all vital-signs Observations by LOINC, take
      most recent per LOINC, render in the original's BP→Temp→Method→
      Pulse→Resp→O2 sequence with all non-empty values."

**2. Implementation fixes (after spec is approved):**
   1. `web/src/components/cards/VitalsCard.tsx`: add fixed row order,
      "Most recent vitals from" header, Temp Method extraction (or
      explicit drop-with-comment), trailing "Click here…" link.
   2. `web/src/components/cards/CareTeamCard.tsx:117-124`: remove
      EmptyCard short-circuit. Render `<table>` with `<thead>` always;
      empty `<tbody>` when participants empty. Drop the "all empty
      cards speak the same visual language" comment.
   3. `web/src/app/patient/[id]/page.tsx:78-108`: rewrite grid to
      row 1 = 4-up, row 2 = Care Team full-width, row 3 = single Vitals
      column (left only — out-of-scope right column doesn't render).
   4. (Optional) Patient name link styling, Allergies reaction tooltip-
      only — these are already documented in `parity-audit.md`; pick
      based on owner appetite for full visual fidelity.

**3. Verification step (manual, before claiming done):**
   - Render `/patient/4` against the live OpenEMR locally.
   - Take a screenshot at the same viewport as `00-dashboard-FULL-PAGE-
     gloria.png.png` and diff visually.
   - Click each card's edit pencil — confirm console.log fires (stubs).
   - Curl `Observation?patient=...&category=vital-signs` and walk the
     bundle alongside the rendered Vitals card to confirm same rows,
     same order, same dual-unit Temperature.
   - Browser-test the auth-code flow once (`auth-notes.md:75-81`
     outstanding) — this is overdue regardless.

## What to defer (gaps that can stay open with explicit documentation)

- **Patient.photo / avatar** — open question Q3, both renders silhouette,
  parity holds for synthetic data.
- **Prescriptions loaded shape** — empirically empty for every synthetic
  patient (1.5 verified). Loaded layout cannot be exercised without
  seeding data.
- **Care Team loaded shape** — same; empty everywhere in synthetic data.
  The headers-empty fix (B2) is independently correct regardless of
  whether the loaded row shape is right.
- **`_sort` / `_count` on `/Observation`** (open Q8) — bundle is small
  enough that client-side sort is fine for now.
- **eRx variant** — out of scope.

## Open questions for the owner

1. **Vitals scope of fix.** Are you willing to update inventory + api-map
   to drop the 7-LOINC curation claim, or do you prefer to keep the
   "spec says 7 LOINCs, port matches" framing and document the
   divergence from the original as accepted? The first is the honest
   reimplementation; the second is faster and already half-defended.
2. **Care Team visual treatment.** When we restore the table headers, do
   you want the empty body row to render a single empty `<tr>` (matching
   the screenshot's faint horizontal divider line below the thead) or
   no `<tr>` at all (just `<tbody></tbody>`)? Screenshot 15 ambiguous.
3. **Layout fidelity bar.** Do you want strict three-row mirroring of
   `demographics.php`, or "spirit of the layout" — clinical cards 4-up,
   Care Team below, Vitals somewhere reasonable? Strict mirroring
   requires a `col-md-8` row 3 that for our scope only contains Vitals,
   leaving a lot of empty space.
4. **Auth-code flow.** Should this investigation also cover
   browser-testing the auth-code path before any code fix, or treat that
   as a separate follow-up? It is the one architecturally-untested path
   underneath all of the above.
