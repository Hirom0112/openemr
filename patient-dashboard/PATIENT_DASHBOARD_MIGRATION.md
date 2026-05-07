# Patient Dashboard Port — Migration Defense

## Why we're porting

The existing OpenEMR patient dashboard is a server-rendered PHP page with
a hybrid loading model. Its entry point — `interface/patient_file/summary/demographics.php` —
ships nine AJAX-loaded `*_fragment.php` files alongside four cards
(allergies, medical problems, medications, prescriptions) rendered
inline as Twig in the same file (`demographics.php:1112–1208`,
`demographics.php:526–533`). Both paths execute `sqlQuery()` and
service-class calls directly from the files that emit HTML. There is
no abstraction over the data layer (see `dashboard-inventory.md` →
"Architecture finding (verified twice against codebase)" → "Rendering
model"). Adding or modifying a card requires touching presentation,
data access, and authorization in the same file.

A FHIR R4 layer exists in parallel under `src/RestControllers/FHIR/`
with controllers for Patient, AllergyIntolerance, CareTeam, Condition,
MedicationRequest, and Observation, but the dashboard does not consume
any of it. Verified by grep across `interface/patient_file/summary/`:
zero references to `/apis/default/fhir/` or any FHIR controller class
(`dashboard-inventory.md` → "FHIR layer relationship"). The two
FHIR-adjacent imports in `demographics.php` are a SMART-launch app
import (line 51) and a portal feature flag boolean (line 1578) —
neither calls the FHIR API.

The auth model — PHP session cookies plus CSRF tokens embedded in
rendered HTML — is correct for an in-process server-rendered app and
incompatible with a decoupled frontend. The port is the work of
introducing a typed data-access layer between presentation and the
database, replacing session+CSRF with OAuth2/OIDC against OpenEMR's
existing OAuth server (using SMART scopes), and collapsing the hybrid
fragment / inline-Twig loading model into a single uniform card pattern.
None of this is cosmetic; each item is a specific finding from the
audit (`dashboard-inventory.md` → "Implications for the port").

## Framework choice: Next.js 16 + TypeScript + Auth.js + shadcn/ui + Tailwind

The choice was not made in the abstract. It was made by mapping each
problem the audit identified to a specific framework capability. Every
selection below traces back to an audit finding documented in
`dashboard-inventory.md`.

### The five problems the audit identified

1. **Presentation coupled to data access.** The original dashboard's
   cards both render HTML and execute SQL queries from the same files.
   No data-layer abstraction exists.

2. **Auth model incompatible with a decoupled frontend.** Session
   cookies + CSRF tokens work for server-rendered apps in the same
   process; they don't work for a separate frontend talking to an API.

3. **Hybrid card-loading model with no uniform pattern.** Nine cards
   AJAX-load HTML fragments; four cards render inline as Twig in the
   dashboard's entry-point file. Both pull from the database directly.

4. **FHIR token security.** A decoupled frontend that handles clinical
   data must not expose the FHIR access token to the browser, where
   third-party scripts could read it.

5. **Missing FHIR resource (MedicationStatement).** This OpenEMR build
   does not implement MedicationStatement. The Medications and
   Prescriptions cards must be synthesized from MedicationRequest with
   filter logic — synthesis logic that has to be exactly right or the
   cards display wrong clinical data.

### Why Next.js 16 with TypeScript solves these

**Server Components solve problems #1 and #4.** A Server Component runs
only on the server. Its code is never bundled to the browser. The FHIR
access token is read server-side, used to fetch FHIR data, and only the
rendered HTML reaches the user. The mechanism: every card is an `async`
Server Component (no `"use client"` directive in the fetch path). The
typed FHIR client is imported only by Server Components, so a `"use
client"` regression on a card would surface as a Next.js build error
("Importing a Server-only module into a Client Component") rather than
a silent token leak.

**One uniform card pattern solves problem #3.** All seven cards extend a
single `<Card>` component (scaffolded from shadcn/ui) with consistent
loading, empty, and error states. The original dashboard's hybrid model
collapses into one pattern in the port. Adding new cards is a single
file with no auth wiring, no database queries, and no template
selection — the data layer, auth, and component model are all uniform.

**TypeScript solves problem #5.** The MedicationRequest synthesis logic
filters by `intent` and `status` to derive both Medications and
Prescriptions cards from the same FHIR resource. TypeScript verifies
the filter logic against the narrow FHIR R4 type slices we hand-maintain
in `web/src/lib/fhir/types.ts` (literal-union enums for `status` /
`intent` / category) at compile time.
Wrong field name, missing case in an enum exhaustiveness check, or
mismatched shape between the synthesis output and the component prop —
all caught before runtime. The original architecture's PHP+SQL layer
has no equivalent compile-time guarantee.

### Why Auth.js solves problem #2

Auth.js is the JavaScript auth library with a documented Generic
OAuth Provider pattern that handles arbitrary OAuth2/OIDC servers.
OpenEMR is not a pre-built provider like Google or GitHub, so the
library's flexibility matters. The library handles authorization code
flow, token storage in encrypted server-side sessions (token off the
browser, again), and automatic refresh on 401. The Auth.js side of the
flow needs only `NEXTAUTH_URL` and the OAuth client credentials to move
between dev and production; the OpenEMR side requires re-registering
the client and disabling the password grant (see
`auth-notes.md` → "Production posture").

When the OAuth flow breaks at deployment — redirect-URI scheme mismatch,
cookie SameSite under HTTPS, PKCE on a server that didn't expect it —
Auth.js's Generic OIDC provider has well-trodden recipes for each.
The session+CSRF model in the original is replaced by standard
OAuth2/OIDC with SMART scopes for FHIR access.

### Why shadcn/ui and Tailwind for the UI

The original dashboard's UI is dense, professional, clinical. shadcn/ui
scaffolds Tailwind components into the repository as editable code
rather than imported library dependencies. The components do not
impose opinionated styling that would fight the original's clinical
density.

One Card component is built once and reused by all seven sections. The
loading skeleton, empty state, and error display are consistent across
the dashboard. This is the uniform pattern problem #3 required, and it
ships in roughly 200 lines of TypeScript.

### Why Railway for deployment

The Co-Pilot project's OpenEMR fork is already deployed on Railway.
Reusing the same platform avoids learning a new deployment surface in
week 5 of a sprint. Railway's internal networking allows the dashboard
service to reach the OpenEMR service over a private network rather
than the public internet — faster, more secure, and the FHIR token
exchange does not traverse the open web.

The dashboard deploys as a new service in the same Railway project
as OpenEMR. Environment variables are set in Railway's dashboard.
The OAuth client registered in the deployed OpenEMR points at the
dashboard's Railway-issued callback URL. The deployment is bounded
in scope and predictable in failure modes.

### What we considered and rejected

**SvelteKit + TypeScript.** Smaller bundles, simpler reactivity model,
and `+page.server.ts` solves the token-safety problem cleanly. Rejected
because the auth library precedent is thinner — when the OAuth flow
hits an edge case at deployment, the documented escape hatches exist
in the React + Auth.js ecosystem more than in the Svelte + @auth/sveltekit
ecosystem. For a five-day sprint with a graded deployment requirement,
ecosystem depth beats architectural elegance.

**Remix / React Router 7 + TypeScript.** Cleaner data-loading model
than Next.js, same React ecosystem. Rejected because the Remix → React
Router 7 transition has caused documentation inconsistencies that would
add friction for a developer new to the ecosystem. The architecture is
slightly cleaner; the documentation is meaningfully worse.

**HTMX + Go or FastAPI.** Genuinely modern in a non-React direction
with strong defense angles for "modern doesn't have to mean SPA."
Rejected because debugging HTMX issues against an OAuth-protected
backend has thinner precedent than the React equivalents. The defense
angle is strong; the operational risk is higher than this sprint can
absorb.

### What we gave up

Bundle sizes will be larger than SvelteKit's by an estimated 50–100KB.
For a low-traffic clinical dashboard, this delta is not perceptible to
users and does not affect grading. The tradeoff is accepted.

The Next.js App Router has documented rough edges, particularly around
caching layers (router cache, fetch cache, full route cache). One
budgeted hour for caching-related debugging is included in the sprint
plan.

TypeScript adds a learning curve for a developer new to it. This curve
is real but pays back within two days through compile-time error
catching. The alternative — shipping JavaScript and debugging shape
mismatches at runtime — is more expensive over a five-day sprint, not
less.

### What this stack does not solve

The audit identified one problem this stack cannot solve at the framework
level: the missing MedicationStatement controller in OpenEMR's FHIR layer.
That gap is documented as an explicit constraint of the underlying
backend, with a synthesis approach using MedicationRequest filters. No
framework choice closes a backend gap; the framework choice only
determines how cleanly the synthesis is expressed in code. TypeScript
makes the synthesis verifiable at compile time, which is the most a
frontend stack can do for a missing backend resource.

---

## What we kept

The port reuses everything on the OpenEMR side that is already working
and within scope. No backend changes ship with this project.

- **OpenEMR's FHIR R4 server.** All six in-scope cards consume existing
  controllers under `src/RestControllers/FHIR/`. Verification 1.5
  confirmed all six endpoints reachable and well-shaped (CareTeam
  reached but `total=0` for every synthetic patient; loaded shape
  inferred only) against the running
  OpenEMR build — the resources the port needs are present and
  responsive. The single confirmed gap (MedicationStatement) is
  documented separately and handled by synthesis, not by patching the
  backend.
- **OpenEMR's existing OAuth2/OIDC server** at `/oauth2/default/*`
  with SMART-on-FHIR scopes. The port registers a new client against
  that server; it does not modify the auth surface.
- **The card set and clinical hierarchy from the original dashboard.**
  Patient header plus six cards (Allergies, Medical Problems,
  Medications, Prescriptions, Care Team, Vitals) — same logical
  groupings, same left-column / right-column placement intent, same
  field set per card as documented in `dashboard-inventory.md`.
- **Visual density.** The original is dense, professional, and clinical
  by design. The port preserves that density rather than adopting a
  consumer-app aesthetic. Card titles, row layouts, and per-row content
  match the field tables in `dashboard-inventory.md` for each card
  (e.g. Allergies fields at `dashboard-inventory.md` "Allergies →
  Fields"; Vitals fields at "Vitals → Fields").

The 17 out-of-scope dashboard sections (`dashboard-inventory.md` →
"Scope") are also kept — by being left alone in OpenEMR. They remain
available through the original interface; the port simply does not
re-implement them.

## What we changed

Each change maps to a specific audit finding, not a stylistic preference.

- **Rendering model: PHP + Twig hybrid → React Server Components.**
  Audit finding: `demographics.php:526–533` (AJAX fragments) and
  `demographics.php:1112–1208` (inline Twig) constitute two parallel
  rendering paths in one file. The port collapses both into one Server
  Component pattern. The FHIR access token stays server-side by
  construction — Server Components never bundle to the browser — which
  is enforced by the framework rather than by developer discipline.
- **Data access: `sqlQuery()` from presentation files → typed FHIR
  client at `lib/fhir/client.ts`.** Audit finding:
  "Both paths hit the database directly via `sqlQuery()` and service
  classes. No abstraction over the data layer"
  (`dashboard-inventory.md` → "Rendering model"). The port routes every
  card through a single FHIR client module with TypeScript types
  generated against FHIR R4. Cards consume parsed resources, never raw
  wire format or SQL rows.
- **Auth: session cookies + CSRF → OAuth2/OIDC via Auth.js.** Audit
  finding: session+CSRF assumes shared process state and embedded
  rendered HTML, neither of which exists across a decoupled frontend
  and backend (`dashboard-inventory.md` → "Implications for the port",
  item 2). The port uses Auth.js's Generic OAuth Provider against
  OpenEMR's `/oauth2/default/*` endpoints with SMART scopes. Tokens
  live in encrypted server-side sessions; the browser never holds a
  raw FHIR access token.
- **Medications / Prescriptions: synthesized from MedicationRequest by
  `intent` and `status`.** Audit finding: MedicationStatement is not
  implemented in this OpenEMR build — no controller in
  `src/RestControllers/FHIR/`, no route in
  `apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php`
  (`dashboard-inventory.md` → "Confirmed gap: MedicationStatement").
  The port synthesizes the two cards from one resource: Medications
  filters `status ∈ {active, on-hold, completed}`; Prescriptions
  filters `intent=order` with a recorded `requester`. Verification 1.5
  (2026-05-07) showed `intent=order` returns zero rows across pids 4,
  5, 13, 24, 26, 27 — every entry emits `intent=plan` with `requester`
  absent. The Prescriptions card therefore renders empty for every
  synthetic patient, matching the original dashboard's "None" state
  for Gloria Tran. This is a property of the synthetic dataset, not a
  defect in the synthesis logic.

---

# Defense material — drafted on Day 1 (kept for reference)

These paragraphs were captured during the architecture audit phase, while
the findings were fresh and the codebase evidence was concrete. They
were the source material for the polished defense above; preserved here
as historical context.

The architecture findings these paragraphs draw from are documented in
`dashboard-inventory.md` under "Architecture finding (verified twice
against codebase)" and were validated by two independent passes through
the OpenEMR source.

---

## Draft 1 — Data layer decoupling

The current OpenEMR patient dashboard couples presentation to data
access at the file level. The dashboard's entry point is
`interface/patient_file/summary/demographics.php`, a single PHP file
that handles two different rendering models for its cards. Nine sections
AJAX-load via `*_fragment.php` files that emit raw HTML directly into
the page. Four clinical cards — allergies, medical problems,
medications, and prescriptions — render inline within demographics.php
itself, using Twig templates rendered from the same file at lines
1112–1208. Both paths hit the database directly via `sqlQuery()` and
service classes. There is no abstraction over the data layer; the
dashboard's HTML output and its database queries live in the same files.

This means the original dashboard cannot be cleanly modified without
touching presentation, data access, and authorization logic in the
same change. Adding a new card requires either creating a new fragment
file with its own database queries, embedded HTML, and CSRF handling,
or extending the inline Twig section in demographics.php with a new
service call and template render. The two patterns are not unified.

This port routes all data through the FHIR R4 API. Every card consumes
a typed FHIR resource through a single client module
(`lib/fhir/client.ts`). The data layer is uniform across all cards: the
header, the five required clinical cards, and the additional Vitals
section all share the same fetch, error-handling, and token-refresh
behavior. Adding a new card in this architecture means writing one
component that calls one typed client method — no new auth wiring, no
new database queries, no new routing. The data layer is decoupled from
presentation by design.

This is an architectural improvement enabled by the framework migration,
not a cosmetic one. The brief explicitly required FHIR consumption,
which means the decoupling is not an optional benefit — it is required
work that the framework either makes natural or makes painful. The
chosen framework makes it natural.

---

## Draft 2 — Auth migration as real engineering work

The original dashboard uses PHP session cookies plus CSRF tokens for
authentication and request authorization. This is appropriate for a
server-rendered application running inside the same PHP process as the
auth layer: the session is shared between request handler and
template, the CSRF token is embedded directly in rendered HTML,
and the database connection inherits the authenticated user's
permissions implicitly. None of this is portable.

A decoupled frontend cannot share a session with the backend in the
same way. The session must be established explicitly through a flow
the backend agrees with. CSRF protection cannot rely on rendered HTML
because the rendered HTML lives in a different process. Token
acquisition, storage, refresh, and revocation all become first-class
concerns instead of side effects of the rendering pipeline.

This port replaces session+CSRF authentication with OAuth2/OpenID
Connect against OpenEMR's existing OAuth server, using SMART scopes
for FHIR resource access. The full flow — authorize, callback, token
exchange, refresh, logout — is implemented through Auth.js
configured against OpenEMR's `/oauth2/default/*` endpoints. Access
tokens are stored server-side in encrypted session cookies; the
browser never holds a raw FHIR token. Token expiry triggers an
automatic refresh through the FHIR client's 401 handler before the
request fails to the user.

This is non-trivial migration work. The original auth model assumes
shared process state; the port assumes neither client and server are
co-located, nor that they share storage of any kind. The auth library
selection was a primary factor in the framework decision: the OAuth
flow against OpenEMR has to work in production, not just demo well,
and the cost of getting auth wrong in a clinical application is high.
The framework choice traded other considerations against the maturity
of its OAuth/OIDC ecosystem and the safety of its server-side session
handling.

---

## Draft 3 — The parity gap I'm choosing to document

OpenEMR's FHIR R4 layer covers most of the resources required for this
port: Patient, AllergyIntolerance, Condition, MedicationRequest,
CareTeam, and Observation. Each has a controller in
`src/RestControllers/FHIR/` and a registered route in
`apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php`.
MedicationStatement is the exception — not implemented in this
build. No controller exists in the controller directory; no route
exists in the route registry. This was verified by direct grep across
both locations.

The original dashboard renders separate "Medications" and
"Prescriptions" cards. In a complete FHIR implementation, these would
map to MedicationStatement (medications the patient is currently
taking, regardless of source) and MedicationRequest (prescriptions
written through this system) respectively. The semantic distinction
matters clinically: a patient on warfarin from an outside cardiologist
appears in the medications list but not the prescriptions list, and
the prescriber's name should differ between the two views.

With only MedicationRequest available, this port synthesizes the
distinction by filtering on the `intent` and `status` fields. The
Medications card displays MedicationRequests with `status` in
{active, on-hold, completed}, regardless of intent — this approximates
"all current medications however they got here." The Prescriptions
card filters more tightly: `intent=order` and a recorded `requester`,
which approximates "prescriptions actively written through OpenEMR's
prescribing workflow." Both cards consume the same FHIR endpoint with
different query parameters.

**Empirical verification (2026-05-07):** A live curl pass against the
local OpenEMR build confirmed that **zero `MedicationRequest` rows
have `intent=order`** across all six sampled synthetic patients (pids
4, 5, 13, 24, 26, 27). Every entry emits `intent=plan` with `requester`
absent. The Prescriptions card therefore renders empty for every
synthetic patient — matching the original dashboard's "None" state.
This is a property of the synthetic dataset, not a defect in the
filter logic; a real OpenEMR install with prescribing-workflow data
would populate the card correctly.

This synthesis is a documented compromise, not a choice I would make
if MedicationStatement were available. A complete implementation would
require either implementing the missing MedicationStatement controller
in OpenEMR (out of scope per the brief — "you are not touching the
backend") or a domain-specific synthesis layer that goes beyond
field-level filtering. Neither is part of this port. The compromise is
called out here so a grader, a future maintainer, or a clinician using
the deployed app understands that the two cards are filtered views of
the same underlying resource, not distinct resources as the FHIR spec
intends.

---

## Draft — Read-only by design

This port is read-only. Every card renders data from FHIR GET requests;
no mutation endpoints are called. The brief scopes the deliverable to
the dashboard's display layer — "pulling live data from the FHIR API"
— and does not require porting the original dashboard's edit
affordances (the pencil icons on each card).

The edit affordances are preserved visually as stubbed buttons that
log a TODO when clicked, so a grader or reviewer can see the
interaction surface the original provides. Implementing those
mutations would require either using FHIR write endpoints (which
have partial support in this OpenEMR build — for example, FHIR Binary
POST returns 404, and certain resources lack write controllers) or
extending OpenEMR with a custom write surface. Both are out of scope
per the brief, which explicitly states "you are not touching the
backend."

A complete editable port would be a meaningfully larger project. The
brief deliberately scopes around it so the framework migration can
be evaluated on its own merits without the additional surface area
of mutation handling.

---

## Known limitations and future work

These are real gaps in the deployed port that a grader, maintainer,
or clinician should be aware of. Each item names the cause and
points to the source of the constraint.

### Known limitations

- **No `MedicationStatement` controller on this OpenEMR build.** Both
  Medications and Prescriptions consume `MedicationRequest` and split
  client-side on `intent` (see "What we changed" → MedicationStatement
  entry, and `dashboard-api-map.md:149`).
- **Prescriptions card is empty for every synthetic patient.** Every
  `MedicationRequest` on this build emits `intent=plan`; zero rows
  match the `intent=order` filter across pids 4, 5, 13, 24, 26, 27
  (`dashboard-api-map.md` → 1.5 verification → Q2,
  `dashboard-api-map.md:319`).
- **AllergyIntolerance falls back to `text.div` when `code` is a
  data-absent-reason.** Handled by the `allergyDisplay()` helper in
  `web/src/components/cards/AllergiesCard.tsx`; confirmed against live
  bundles in 1.5 (`dashboard-api-map.md:335`).
- **CareTeam loaded-row shape is inferred, not validated.** The
  `care_teams` / `care_team_member` tables are empty across all 27
  synthetic patients (`dashboard-inventory.md` → "Synthetic dataset
  gaps", `dashboard-api-map.md:329`). Empty-state and copy match the
  original; the populated layout has not been exercised against live
  FHIR.
- **`Patient.photo` not verified to populate.** A Lucide silhouette
  renders unconditionally; whether OpenEMR's FHIR Patient surfaces the
  document-store avatar is open (`dashboard-api-map.md:63`,
  open question Q3).
- **`_sort` / `_count` on `Observation?category=vital-signs` not
  exercised.** The Vitals card pulls the full bundle and sorts /
  groups client-side. Correct for ~15-row synthetic bundles; will not
  scale (`dashboard-api-map.md:221`, open question Q8).
- **Vitals sub-fields silently omitted.** `Temp Method` and
  `Waist Circumference` have no LOINC in `FhirObservationVitalsService`'s
  projection (`dashboard-api-map.md:246`, `:256`); the port renders
  what the FHIR bundle exposes and drops the rest.
- **Authorization-code OAuth flow is wired but not browser-tested.**
  The 1.5 pass exercised the password-grant code path on the same
  OpenEMR; the auth-code path is structurally different (consent
  screen, PKCE, refresh-token rotation) and has not seen a live click
  yet (`auth-notes.md:75`).
- **Production posture depends on a manual globals flip.**
  `oauth_password_grant=0` must be set on the Railway OpenEMR before
  the deployed instance accepts traffic, or the password grant remains
  available alongside auth-code (`auth-notes.md:64`, `:84`).
- **Encounter picker and "Open Encounter" header controls are
  stubs.** Encounter resources are not fetched; the brief is
  dashboard-only and encounter management is out of scope.
- **Medical Problems renders onset date + status badge that the
  original does not.** Deliberate per brief, but a visual departure
  from strict feature parity (`execution-plan.md` 4.3 deviation
  note, line 228).

### Out-of-scope by brief

The following are deliberate exclusions, not gaps:

- The 17 dashboard sections not ported (Demographics edit, Insurance,
  Billing, Labs, Appointments, Immunizations, etc. —
  `dashboard-inventory.md` → "Scope" → "Out of scope").
- Mutation / edit affordances. The card pencil icons are stubs
  (`stats_full.php?...&category=...` in the legacy UI). The port is
  read-only by design — see "Defense material — drafted on Day 1"
  and the per-card pencil stubs.
- Backend changes to OpenEMR. The brief is explicit that the backend
  is not in play; every workaround above is a client-side
  accommodation.

### Future work

Concrete next steps, ordered by likely value:

- **Register the OAuth client through OpenEMR's Admin → System → API
  Clients UI on Railway**, replacing the current SQL flip of
  `client_role` (`auth-notes.md` production-posture checklist). This
  exercises the audit trail and scope-grant flow.
- **Implement a `MedicationStatement` controller in OpenEMR**
  (`src/RestControllers/FHIR/`) and a route in the FHIR config. Closes
  the synthesis compromise at the source. Out of scope today; the
  right long-term fix.
- **Per-record edit affordances.** Wire the pencil-icon stubs to FHIR
  write endpoints with appropriate write-side scopes (`user/*.write`,
  `patient/*.write`).
- **Port the remaining 17 dashboard sections** through the same
  Server-Component / `*ViewCard` pattern.
- **Add a Playwright or Cypress test for the auth-code round trip.**
  The 85 unit tests do not cover the redirect → token-exchange →
  refresh path; this is the single largest untested integration
  surface.
- **Memoize the `pid → uuid` lookup at the page level.** Next's RSC
  fetch cache dedupes within a request, but explicit memoization would
  remove a duplicate `Patient?identifier=` call on every page load
  (header + body).
- **Server-side trim the Observation bundle.** Investigate
  `_sort=-date&_count=1` against `/Observation` so the Vitals card
  pulls only the most-recent row rather than the full bundle.
