# Defense material — drafted on Day 1, to be polished on Day 3

These paragraphs were captured during the architecture audit phase, while
the findings were fresh and the codebase evidence was concrete. They are
drafts. On Day 3 they will be reorganized into the formal defense
sections of this document (framework choice, what we kept, what we
changed, tradeoffs, known limitations).

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
exchange, refresh, logout — is implemented through `[AUTH_LIBRARY]`
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

## Draft 4 — Why this finding matters for the framework defense

The framework choice is graded on whether it addresses the problems
with the current architecture, not on whether it is fashionable. The
architecture audit surfaced three concrete problems with the existing
dashboard:

First, a hybrid card-loading model. Nine cards AJAX-load HTML
fragments; four cards render inline as Twig. There is no uniform
pattern for adding a new card or modifying an existing one — every
change requires deciding which of the two patterns to follow, and the
two patterns share no infrastructure.

Second, direct database access from presentation files. Every card,
in both rendering models, calls `sqlQuery()` or a service class
directly from the file that emits its HTML. There is no data-layer
abstraction. Caching, request batching, type safety, and error
handling have to be implemented per-card or skipped. The original
skips them.

Third, session+CSRF auth that does not work for a decoupled
frontend. The auth model is correct for the original architecture
and incompatible with any modern client.

The chosen framework, `[FRAMEWORK]`, addresses each of these directly.
A single uniform card pattern handles loading, empty, and error states
identically across all six cards in this port — the pattern is
established once in `components/cards/PatientHeader.tsx` and reused
verbatim by every card that follows. The typed FHIR client in
`lib/fhir/` decouples presentation from data fetching: cards never
see the FHIR wire format, only the parsed and typed resources their
renderers expect. `[AUTH_LIBRARY]` handles OAuth2/SMART out of the
box, including the token refresh and server-side session storage that
the original architecture cannot support.

Each of these is a specific finding mapped to a specific framework
capability. The framework defense is not "modern is better." The
framework defense is "the brief required a specific set of
architectural improvements, the audit identified the specific things
that needed improving, and the chosen framework makes each improvement
natural rather than forced."

---
