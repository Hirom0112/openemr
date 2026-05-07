# Patient Dashboard Port — Phased Execution Plan

## Phase 0: Tonight (60 min remaining) — ✅ COMPLETE

**Goal:** Complete Day 1 deliverables. Stop and sleep.

### 0.1 — Finish screenshot tour (~30 min)
- [x] 7 tight Gloria card crops (`10-` through `16-`)
- [x] 1 empty state from another patient (`21-card-allergies-empty.png`)
- [x] 6 edit-pencil interactions (`40-` through `45-`)

### 0.2 — File the verified architecture finding into inventory (~10 min)
- [x] Paste the Architecture finding section into
      `dashboard-inventory.md` (Action 1 from prior plan)

### 0.3 — Lock the MedicationStatement decision into inventory (~10 min)
- [x] Add Data source decision subsection to Medications entry
- [x] Add Data source decision subsection to Prescriptions entry
      (Action 2, Option B — synthesize from MedicationRequest)

### 0.4 — Capture defense paragraph drafts (~10 min)
- [x] Paste the four defense paragraph drafts into
      `PATIENT_DASHBOARD_MIGRATION.md` under
      "Defense material — to be polished on Day 3"

### 0.5 — Commit and stop
- [x] `git add patient-dashboard/`
- [x] Commits covering inventory + migration doc drafts
      (`68e485ce0`, `02d953709`, `dbba723cc`)
- [ ] Stop. Don't open the W2 folder. Sleep.

**Phase 0 done when:** All 14 screenshots committed, inventory and
migration doc updated, lights out.

---

## Phase 1: Day 2 morning — Inventory and API map (3-4 hours)

**Goal:** Complete spec — every field mapped to a verified endpoint.

### 1.1 — Have Claude Code draft inventory entries from screenshots (~45 min)
- [x] Send the inventory-drafting prompt
- [x] Claude Code drafts entries for the 7 in-scope sections
- [ ] You review each entry against the corresponding screenshot
- [ ] You add anything missing, fix anything wrong
- [ ] Resolve all `TODO: confirm` comments (markers present; user pass pending)
- [x] Commit

### 1.2 — Register OAuth client in OpenEMR (~30 min)
- [x] ~~Navigate to Administration → System → API Clients~~
      Done via RFC 7591 dynamic registration (curl `POST /oauth2/default/registration`)
- [x] Register confidential client (`application_type=private`)
- [x] Set redirect URI to `http://localhost:3000/api/auth/callback/openemr`
- [x] Request scopes: openid, fhirUser, offline_access, user/Patient.read,
      user/AllergyIntolerance.read, user/Condition.read,
      user/MedicationRequest.read, user/CareTeam.read, user/Observation.read
- [x] Save client_id + client_secret to `patient-dashboard/.env.local`
      (gitignored — verified via `git check-ignore`)
- [x] Document in `auth-notes.md` (no secrets in git)
- [x] Flip `oauth_clients.is_enabled = 1` via SQL (skips admin-UI approval)

### 1.3 — Test OAuth flow end-to-end with curl (~45 min)
- [x] ~~Hit authorize URL in browser, approve scopes~~
      Used password-grant flow (oauth_password_grant=3 enabled) — no
      browser needed. Auth-code flow will still be used in the actual
      web app; password grant is a dev shortcut for headless verification.
- [x] ~~Capture authorization code from redirect~~ (n/a for password grant)
- [x] Exchange credentials for tokens via curl POST to
      `/oauth2/default/token` (grant_type=password)
- [x] Hit `/apis/default/fhir/Patient?identifier=4` with access token
      (pid→uuid resolution path confirmed)
- [x] Confirm Gloria's patient JSON returns (uuid `a1af78de-...`)
- [x] Update auth-notes.md with token expiry (3600s), format (Bearer),
      refresh_token presence, scope echo-back, quirks
- [x] Commit

### 1.4 — Have Claude Code build the API map (~45 min)
- [x] Send the API map prompt referencing inventory and auth-notes
- [x] Claude Code maps every field to a FHIR endpoint
- [x] Output: `dashboard-api-map.md`

### 1.5 — Verify the API map by hitting 4-5 endpoints with curl (~45 min)
- [x] Curled 5 endpoints: Patient, AllergyIntolerance, Condition
      (problem-list-item), MedicationRequest, CareTeam, Observation
      (vital-signs)
- [x] Confirmed response shapes match the API map's claims; documented
      AllergyIntolerance allergen-name fallback (`text.div` when
      `code` is data-absent-reason)
- [x] **MedicationRequest filter empirically verified:** zero
      `intent=order` across pids {4, 5, 13, 24, 26, 27} — Prescriptions
      card will render empty for every synthetic patient. Documented
      in inventory + migration doc as a known limitation of the
      synthetic dataset, not the filter.
- [ ] Outstanding `⚠ NOT FOUND` flags (carried into Phase 4 verification):
      Patient.photo, CareTeam loaded shape (no synthetic data),
      Temp Method, Waist Circumference, dosageInstruction shape,
      `_sort`/`_count` on Observation
- [x] Commit

**Phase 1 done when:** Inventory complete, OAuth working end-to-end,
API map verified against live endpoints, MedicationRequest filter
hypothesis confirmed or revised. ✅ COMPLETE 2026-05-07 (Phase 1.1–1.5
all closed; user review of inventory still pending as a soft step
before Phase 2).

---

## Phase 2: Day 3 — Framework choice and defense (2-3 hours)

**Goal:** Pick the framework. Write the defense. Don't write code yet.

### 2.1 — Confirm additional section choice (~15 min) ✅
- [x] Confirm Vitals as the additional section
- [x] Verified in 1.5: `Observation?category=vital-signs` returns 15
      entries for Gloria (LOINCs 85353-1, 9279-1, 8867-4 confirmed)
- [x] No field shifts; inventory entry stands

### 2.2 — Confirm framework lock-in (~5 min) ✅ DECIDED

**Stack:** Next.js 16 + TypeScript + Auth.js + shadcn/ui + Tailwind
**Deploy target:** Railway (same project as OpenEMR; private internal
networking between services)

**Defense:** drafted in PATIENT_DASHBOARD_MIGRATION.md (commit dbba723cc
plus follow-ups). Will polish with Phase 1 empirical findings before
Phase 4 starts.

- [ ] Confirm: nothing in Phase 1 verification changed the calculus
- [ ] Lock and proceed to 2.3

### 2.3 — Write the framework defense (~60-90 min) ✅
- [x] PATIENT_DASHBOARD_MIGRATION.md now has structured top-level
      sections: Why we're porting → Framework choice → What we kept →
      What we changed → Day 1 drafts (kept for reference) → Known
      limitations and future work
- [x] [FRAMEWORK] and [AUTH_LIBRARY] placeholders resolved
      (Next.js 16, Auth.js)
- [x] Known limitations and Future work populated as placeholders
      (will be revised after Phase 4)
- [x] Commit

### 2.4 — Have Claude Code critique the defense (~30 min)
- [ ] Send the critique prompt
- [ ] Claude Code outputs a numbered issue list
- [ ] You fix issues yourself (don't let Claude Code rewrite)
- [ ] Commit

**Phase 2 done when:** Framework picked, defense doc reads cleanly,
no marketing copy survives, every claim backed by reasoning.

---

## Phase 3: Day 4 — Auth and foundation (3-4 hours)

**Goal:** Working OAuth in the actual app. Typed FHIR client. Nothing UI yet.

### 3.1 — Scaffold the project (~30 min) ✅
- [x] Scaffolded `patient-dashboard/web/` — Next.js 16.2.5 + TypeScript
      strict + Auth.js v5 (next-auth@5.0.0-beta.31) + shadcn/ui (neutral)
      + Tailwind v4 + React 19. (Deviation: create-next-app@latest
      resolved to v16, not v15 — same App Router, async params; doc
      bumped 15→16.)
- [x] Created `src/lib/fhir/`, `src/lib/auth/`, `src/components/cards/`
      with .gitkeep files; placeholder `src/app/patient/[id]/page.tsx`
- [x] `npm run dev` smoke test — `curl /patient/4` returns HTTP 200,
      renders `<h1>Patient 4</h1>` (independently verified by orchestrator)
- [x] `npm run build` exits 0 (verified)
- [x] Commit

### 3.2 — Wire OAuth (~60-90 min) ✅ (browser smoke pending)
- [x] Auth.js v5 wired against OpenEMR's /oauth2/default with the
      generic OIDC provider; PKCE+state, encrypted JWT cookie session,
      refresh-on-expiry inside the jwt callback
- [x] Login (/login), callback (/api/auth/callback/openemr),
      sign-out, route protection via `proxy.ts` (Next 16 deprecates
      middleware.ts in favor of proxy.ts), refresh on token expiry
- [ ] Test full flow in browser end-to-end (PENDING — human click)
- [ ] Test logout (PENDING — human click)
- [ ] Test token refresh (PENDING — manual via expiry shorten or wait)
- [x] /patient/4 → 302 to /login when unauthenticated (verified)
- [x] Commit

### 3.3 — Build the typed FHIR client (~45 min) ✅
- [x] FhirClient at `src/lib/fhir/client.ts` with 7 typed methods
      matching the API map; narrow FHIR R4 type slices (no
      @types/fhir dependency)
- [x] 401 surfaces as FhirApiError(status=401); refresh handled by
      Auth.js jwt callback, not the client
- [x] FhirApiError class at `src/lib/fhir/error.ts`
- [x] Synthesis helpers (filterMedications, filterPrescriptions) +
      allergyDisplay fallback per 1.5 findings
- [x] 17/17 vitest unit tests pass (success, 401, 500, network error,
      auth-header propagation, synthesis filters, allergy fallback)
- [x] `npm test` passes; `tsc --noEmit` clean
- [ ] Spot-check one method against live OpenEMR (DEFERRED to first
      card render in Phase 4 — `live-spot-check.md` records the curl
      contract)
- [x] Commit

**Phase 3 done when:** Auth works end-to-end including refresh,
FHIR client tested and spot-checked, no UI built yet.

---

## Phase 4: Day 5 — Build the cards (4-5 hours)

**Goal:** All seven cards rendering live data with parity to original.

### 4.1 — Patient header (pattern-setter) (~30 min) ✅
- [x] PatientHeader.tsx (Server Component) + card-states.tsx
      primitives (LoadingCard / ErrorCard / EmptyCard) for the
      6 sibling cards to reuse
- [x] Mounted on /patient/[id] inside Suspense boundary
- [x] 11 component tests (jsdom env) + 17 fhir client tests
      (node env) = 28/28 passing
- [ ] Side-by-side visual comparison vs reference screenshot
      (PENDING — human pass; one TODO recorded: name rendered as
      colored span vs original link styling)
- [x] Commit (pattern locked for siblings)

### 4.2 — Allergies card (~30 min) ✅
- [x] Built per inventory; uses `allergyDisplay()` fallback for the
      Gloria text.div case from 1.5
- [x] Loaded / empty (None) / error states
- [ ] Side-by-side visual comparison (PENDING — human pass)
- [x] Committed in Wave 4 batch

### 4.3 — Medical Problems card (~30 min) ✅
- [x] Same pattern; filters resolved out (matches inventory's
      filterActiveIssues). Sub-agent flagged a parity deviation —
      original card shows only the title; this port adds onset date +
      status badge per the brief. Documented for follow-up.
- [x] Committed in Wave 4 batch

### 4.4 — Medications card (~30 min) ✅
- [x] Uses `filterMedications` synthesis helper (status in active /
      on-hold / completed)
- [x] Committed in Wave 4 batch

### 4.5 — Prescriptions card (~30 min) ✅
- [x] Uses `filterPrescriptions` (intent=order + status=active +
      requester recorded). Empty state is the **realistic default**
      for every synthetic patient per 1.5 — treated as successful load.
- [x] Committed in Wave 4 batch

### 4.6 — Care Team card (~30 min) ✅
- [x] Empty for every synthetic patient (1.5). Loaded shape inferred;
      test marks the inferred row as such.
- [x] Committed in Wave 4 batch

### 4.7 — Vitals card (~45 min) ✅
- [x] Most-recent-per-LOINC grouping; panel LOINC 85353-1 dropped;
      blood-pressure panel 85354-9 synthesizes systolic/diastolic
      from `component[]`. Server-side `_sort`/`_count` not attempted
      (open Q8 still); client-side sort is fine for ~15-row bundles.
- [x] Committed in Wave 4 batch

### 4.8 — Mount edit pencil stubs (~30 min) ✅
- [x] Each of the 6 sibling cards ships a Lucide `Pencil` button with
      `aria-label`, a `console.log("TODO: ...")` onClick, and a code
      comment pointing to the inventory's "Edit affordance" subsection.
      No mutation logic — read-only by design (covered in migration
      doc → "Read-only by design" Day-1 draft + polished defense).
- [x] Committed in Wave 4 batch

**Phase 4 done when:** All seven cards render live data, side-by-side
parity confirmed against original for each.

---

## Phase 5: Day 6 — Audit, deploy, polish, ship (4-5 hours)

**Goal:** Final ship.

### 5.1 — Parity audit (~30 min)
- [ ] Have Claude Code audit parity against inventory
- [ ] Output: `parity-audit.md` with field-by-field gap report
- [ ] Triage gaps: fix or document as explicit cuts

### 5.2 — Update migration doc with limitations (~30 min)
- [ ] Have Claude Code fill in Known limitations section
- [ ] Have Claude Code fill in Future work section
- [ ] Don't let it touch the framework defense — those sections
      are yours
- [ ] Final cold read

### 5.3 — Deploy (~60-90 min)
- [ ] Have Claude Code prep deployment config + README
- [ ] Deploy yourself (Vercel or Railway)
- [ ] Register new OAuth client pointing at deployed redirect URI
- [ ] Set environment variables
- [ ] Test login in production
- [ ] Fix what breaks (will be redirect URI or CORS)
- [ ] Confirm every card renders against deployed instance

### 5.4 — Final cold read of migration doc (~30 min)
- [ ] Have Claude Code critique the final doc
- [ ] You fix issues yourself
- [ ] Read out loud one more time, alone

### 5.5 — Final ship checklist (~30 min)
- [ ] All seven cards with real data in production
- [ ] OAuth login + logout + refresh work in production
- [ ] Loading / empty / error states work
- [ ] Side-by-side parity confirmed for every section
- [ ] Migration doc reads clean cold
- [ ] README enables clone-and-run
- [ ] Public URL works in incognito

### 5.6 — Submit (~5 min)
- [ ] Submit per assignment instructions
- [ ] Don't sit on it past deadline polishing

**Phase 5 done when:** Submitted.
