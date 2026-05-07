# Patient Dashboard — Port

Modernized read-only patient dashboard for OpenEMR. Reimplementation
of the legacy `interface/patient_file/summary/demographics.php` view
as a Next.js 16 (App Router) app, integrated back into OpenEMR via a
thin custom module that hosts the Next.js bundle in an iframe.

## What's in scope

Per brief: OAuth2/OIDC auth, patient header, five clinical cards
(Allergies, Medical Problems, Medications, Prescriptions, Care Team)
plus one additional section (Vitals). Read-only acceptable.

The brief was clarified mid-project: the dashboard must stay part of
OpenEMR's broader user experience (no standalone app), no backend
changes, OAuth client registration may need manual SQL workarounds.
This implementation lives as a Railway-deployable Next.js app
embedded in OpenEMR via a custom module.

## What's out of scope

| Out-of-scope | Why |
|---|---|
| Edit / mutation flows on any card | Brief calls read-only acceptable for the listed feature subset |
| Encounter management (selection, creation) | Same — read-only chrome |
| Patient search / patient picker | Use URL params (`/patient/{pid}`); OpenEMR drives the picker |
| Cards beyond the required 6 + 1 additional | Scope discipline |
| OpenEMR top chrome (Calendar, Messages, Visit History) | Out of scope; module embeds inside that chrome |
| Pixel-perfect color match to OpenEMR's theme | Spirit-of-the-layout, defended in migration doc |

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  OpenEMR (PHP)                                          │
│  - existing patient chart                               │
│  - new "Dashboard (Port)" tab in top nav                │
│  - oe-module-patient-dashboard-port hosts an iframe →   │
└────────────┬────────────────────────────────────────────┘
             │ iframe src = ${PATIENT_DASHBOARD_URL}/patient/{pid}
             ▼
┌─────────────────────────────────────────────────────────┐
│  Next.js (App Router) patient-dashboard/web             │
│  - Auth.js v5 → OAuth2 auth-code with OpenEMR           │
│  - Server Components fetch FHIR R4 resources server-    │
│    side; access token never reaches the browser         │
│  - 6 clinical cards + Vitals + Patient header           │
└─────────────────────────────────────────────────────────┘
```

Two layers, two repos-of-concern:

- **`patient-dashboard/web/`** — the Next.js app. Self-contained,
  Railway-deployable.
- **`interface/modules/custom_modules/oe-module-patient-dashboard-port/`** —
  the OpenEMR module that registers the menu tab and embeds the
  iframe. Mirrors the existing `oe-module-clinical-copilot` pattern.

## Local development

### Prerequisites

- Docker Desktop running
- Node 22+ (for the Next.js dev server)

### Run

1. Bring up OpenEMR locally:

   ```bash
   cd docker/development-easy
   ./up.sh        # or: docker compose up --detach --wait
   ```

   - OpenEMR: <http://localhost:8300/> or <https://localhost:9300/>
   - phpMyAdmin: <http://localhost:8310/>
   - Login: `admin` / `pass`

2. Install the OpenEMR module. From the OpenEMR admin → Modules →
   Manage Modules: register, install, and enable
   "Patient Dashboard (Port)". Or via SQL:

   ```sql
   UPDATE modules
      SET mod_active = 1, mod_enabled = 1
    WHERE mod_directory = 'oe-module-patient-dashboard-port';
   ```

3. Configure the OAuth client. The dev `.env.local` ships with a
   pre-registered client for the `localhost:9300` instance — no extra
   step needed locally.

4. Run the Next.js dev server:

   ```bash
   cd patient-dashboard/web
   npm install
   npm run dev
   # http://localhost:3000
   ```

5. Open the dashboard:
   - Direct: <http://localhost:3000> → redirects to `/patient/4`
     (Gloria Tran). First hit triggers OpenEMR auth.
   - Embedded: log into OpenEMR at <http://localhost:8300/> → click
     "Dashboard (Port)" in the top nav.

### Run tests

```bash
cd patient-dashboard/web
npm test
```

88 unit tests (vitest + @testing-library/react), all isolated — no
docker required.

## Deployment

See [`DEPLOY.md`](./DEPLOY.md) for the Railway runbook (R4.1 + R4.2).

## Documentation map

| File | Purpose |
|---|---|
| [`README.md`](./README.md) | This file |
| [`DEPLOY.md`](./DEPLOY.md) | Railway deployment runbook |
| [`PATIENT_DASHBOARD_MIGRATION.md`](./PATIENT_DASHBOARD_MIGRATION.md) | Graded artifact: framework defense + architecture findings |
| [`dashboard-inventory.md`](./dashboard-inventory.md) | Per-card spec — fields, position, states, edit affordance |
| [`dashboard-api-map.md`](./dashboard-api-map.md) | FHIR R4 resource → endpoint map per card |
| [`auth-notes.md`](./auth-notes.md) | OAuth2 / Auth.js findings, production posture |
| [`parity-investigation-2026-05-08.md`](./parity-investigation-2026-05-08.md) | Mid-project parity root-cause investigation |
| [`parity-audit.md`](./parity-audit.md) | Per-card audit log |

## Known limitations

| Limitation | Justification |
|---|---|
| Empty Care Team table for every synthetic patient | `care_teams` and `care_team_member` are empty in the seed data; the loaded row shape is inferred from `FhirCareTeamService` |
| Empty Prescriptions card for every synthetic patient | No `MedicationRequest` rows with `intent=order, status=active` and a `requester` set |
| `MedicationStatement` synthesized from `MedicationRequest` | Confirmed in Phase 1 against `FhirMedicationStatementService` source |
| Edit affordances disabled with read-only tooltip | Brief is read-only; the icons are present for parity |
| Encounter controls show toast on click | Same — visible per parity, not wired |
| High-risk allergy highlight not visually exercisable | Synthetic data has no `criticality=high` or `severity=severe`; logic is unit-tested |
| Layout flattens the original's `col-md-8` / `col-md-4` bottom row | Strict mirror would render an empty right gutter for our scope; defended in migration doc |

## Demo

A 3–5 minute walkthrough is included with the submission: login flow,
patient header, each card rendering, the Vitals filtering, navigating
in and out via the OpenEMR module, logout. The defense paragraphs
behind every visible choice live in
[`PATIENT_DASHBOARD_MIGRATION.md`](./PATIENT_DASHBOARD_MIGRATION.md).
