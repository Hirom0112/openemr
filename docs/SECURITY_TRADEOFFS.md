# Security Tradeoffs — Clinical Co-Pilot

This document records security-posture deviations the Clinical Co-Pilot pilot
takes from the architecture described in `W2_ARCHITECTURE.md`. It is the
operator-facing companion to §4.2.1 and §4.2.2 of that document. Read this
before provisioning a new environment, rotating credentials, or evaluating
whether to retire the deviations.

The TL;DR: the agent-api currently writes documents into OpenEMR via a custom
JWT-protected endpoint instead of the FHIR `Binary` POST or the legacy REST
upload, because both of those paths are upstream-limited on the OpenEMR build
we deploy. The deviation is reversible; this document records what to do when
the upstream paths come back.

---

## 1. The custom upload endpoint deviation (summary)

**What ships.** A JWT-protected endpoint inside the existing
`oe-module-clinical-copilot` module:

- URL: `/interface/modules/custom_modules/oe-module-clinical-copilot/public/upload.php`
- Controller: `interface/modules/custom_modules/oe-module-clinical-copilot/src/UploadController.php`
- Auth: HS256 JWT signed with `COPILOT_JWT_SECRET`
- Persistence: OpenEMR's own `Document::createDocument`
- Response: `{documentId: int, patient_id, category_id}`

**Why not the documented paths.**

- **FHIR `Binary` POST.** The deployed OpenEMR's `CapabilityStatement`
  advertises `Binary` as `read` only. `POST /apis/default/fhir/Binary`
  returns `404 Not Found` — the route is not registered in this build.
- **Legacy REST upload `/apis/default/api/patient/{pid}/document`.** Returns
  `401 Unauthorized` for the agent-api's password-grant client even with a
  valid bearer and `api:oemr` requested. See §3 below for the four-gate
  investigation.

**What it preserves.** Documents still land in OpenEMR's `documents` table
and are visible in OpenEMR's native Documents tab UI. Round-trip integrity
(`W2_ARCHITECTURE.md §4.3`), audit dual-target (§9.4), and "OpenEMR is the
system of record" all still hold. (v1: FHIR `DocumentReference` reads
return `total=0` because OpenEMR's `DocumentService::search` filters reads
via `$document->can_access($_SESSION['authUser'])`, and the OAuth bearer's
request session does not bind `authUser` on the deployed OpenEMR build —
so `$username` is `null` and every doc fails the per-row check. The fix
is upstream OAuth-to-PHP-session bridging, not an ACL grant. Derived facts
ARE persisted now — as FHIR-shaped Observations into the module-private
`copilot_observations` MySQL table via a second custom JWT-authenticated
endpoint. See `W2_ARCHITECTURE.md §4.2.1` and §4.2.4.)

**What it deviates on.** The OpenEMR-side authentication for this single
endpoint moves from OAuth bearer + scope check + ACL gate to a shared HMAC
secret. See §2.

**Reversibility.** The custom path is one tier in a four-tier fallback chain.
When the upstream paths become functional, the chain stops landing on tier 3
without any code change. Setting `COPILOT_JWT_SECRET` to empty disables the
tier explicitly.

---

## 1a. The custom Observation endpoint (parallel deviation)

A second JWT-protected endpoint inside the same module persists derived
FHIR-shaped Observations:

- URL: `/interface/modules/custom_modules/oe-module-clinical-copilot/public/observation.php`
- Controller: `interface/modules/custom_modules/oe-module-clinical-copilot/src/ObservationController.php`
- Auth: HS256 JWT signed with the **same** `COPILOT_JWT_SECRET`
- Persistence: module-private `copilot_observations` MySQL table (auto-created on first call)
- Response: `{observationId: "copilot-{doc_id}-{loinc_code}"}` — deterministic, so re-extraction UPSERTs

This endpoint inherits the same JWT-secret blast-radius story as
`UploadController` — the two endpoints share `COPILOT_JWT_SECRET`. A holder
of the secret can mint a valid JWT for either endpoint and write arbitrary
documents OR derived Observations to any patient's chart. The blast radius
of a single leaked secret is therefore the union of both authorities.

The forward-removal path is the same as §1: when OpenEMR's deployed FHIR
`POST /Observation` controller is implemented (currently 404), the
agent-api can fall back to the standard FHIR write surface and the custom
endpoint can be retired. Until then, the FHIR resource shape is preserved
in `copilot_observations` so the migration is a config + mapping change,
not a rewrite. See `W2_ARCHITECTURE.md §4.2.4`.

---

## 2. `COPILOT_JWT_SECRET` handling

### 2.1 Generation

```sh
# 32 random bytes, base64-encoded (recommended minimum entropy)
openssl rand -base64 32
```

The secret must be at least 32 bytes of cryptographic randomness. Do not use
human-chosen passphrases.

### 2.2 Storage

| Environment | Acceptable storage |
|---|---|
| Local dev | `.env` file ignored by git (verify with `git check-ignore`) |
| Pilot (Railway) | Railway environment variables (encrypted at rest, scoped to service) |
| Production | AWS Secrets Manager, GCP Secret Manager, or HashiCorp Vault |

Never check the secret into git. Never bake it into a container image.
Never include it in a plaintext config file that ships in a release artifact.

### 2.3 Distribution

The same value must exist on **both**:

- `copilot-agent-api` (Python service) — used to mint JWTs in `_mint_copilot_jwt` for both the upload and Observation endpoints
- `clinical-copilot-openemr` (PHP / OpenEMR) — used to verify JWTs in **both** `UploadController` and `ObservationController`

Asymmetry between the two services causes all uploads via tier 3 AND all
Observation writes to fail closed: OpenEMR returns `401`, the agent-api
logs the failure, and the upload fallback chain falls through to tier 4
(local-disk). Failed Observation writes do not have a fallback target —
the derived facts simply do not persist on the OpenEMR side until the
secret asymmetry is resolved, and the agent-api logs the failure for
operator visibility. There is no asymmetry case that produces an accepted
unauthorized write on either endpoint.

### 2.4 Rotation

- **Cadence.** Every 90 days, or immediately on any suspicion of leak.
- **Procedure.**
  1. Generate a new secret (`openssl rand -base64 32`).
  2. Set the new value on both `copilot-agent-api` and `clinical-copilot-openemr`.
     The same secret is used to authenticate **both** the upload endpoint
     (§1) and the Observation endpoint (§1a) — there is one secret, not two.
  3. Redeploy in either order. A transient window of `401` responses is
     acceptable on both endpoints; the upload fallback chain handles its
     gap via tier 4 (local-disk), and Observation writes during the gap
     simply do not persist until the next extraction (the deterministic
     id scheme makes that a no-op UPSERT once the secret is in sync).
  4. Verify post-deploy by uploading a test document AND triggering one
     extraction. Confirm a `200` response with a `documentId` from the
     upload endpoint AND a `200` with `observationId` from the Observation
     endpoint, both visible in the agent-api logs.
- **Do not** log or print the old or new secret value during rotation. Verify
  only by observing upload success, not by echoing the secret.

### 2.5 Per-environment isolation

Dev, staging, and production must use **distinct secrets**. A leaked dev
secret must never grant write access to staging or production. Reuse across
environments collapses the blast-radius boundary and is forbidden.

### 2.6 Blast radius

A holder of `COPILOT_JWT_SECRET` can mint a valid JWT and write arbitrary
documents AND arbitrary derived Observations to any patient's chart. There
is no per-patient or per-user authorization gate on either custom endpoint.
This is equivalent to admin-level chart-write authority on the OpenEMR
instance, across both the document table and the module-private
`copilot_observations` table.

### 2.7 Kill switch

Unsetting `COPILOT_JWT_SECRET` (or setting it empty) on the agent-api side
makes `_mint_copilot_jwt` return `None`, which disables **both** custom
endpoints in one shot: tier 3 of the upload fallback chain is skipped, and
Observation writes simply do not happen (the agent-api logs that
persistence was skipped). Use this as the off switch when an environment
should not have agent-api chart-write authority.

---

## 3. Token-cache invalidation behavior

The agent-api caches OAuth bearer tokens in `agent-api/auth/fhir_client.py::get_access_token`.
The cache key is `(client_id, sorted_scopes)` via `_token_cache_key()`. This
means:

- A scope change at the call site causes a cache miss and an automatic
  re-fetch from OpenEMR's `/oauth2/default/token`. The caller does not need
  to manually invalidate.
- A `client_id` change (e.g., switching from password-grant client to a
  different registered client) likewise causes a cache miss and re-fetch.
- Tokens are evicted at expiry (`exp - skew`) regardless of scope.

This matters for the legacy REST fallback investigation (§4 below): when
diagnosing why a request returns `401`, you can be confident that adding or
removing a scope does **not** silently reuse a stale token from a prior
scope set.

---

## 4. The four-gate REST path investigation

This is recorded so future operators do not repeat the dig. The legacy REST
upload `POST /apis/default/api/patient/{pid}/document` returns `401` for the
agent-api's password-grant client. The path is gated by four independent
checks; each one is sufficient to deny on its own. Re-enabling the path
requires fixing **gates 1 (or 3) and 4** together — fixing only one is not
enough.

### Gate 1 — Scope finalization silently drops unregistered scopes

`src/Common/Auth/OpenIDConnect/Repositories/ScopeRepository.php` finalizes
the requested scope set against the client's registered scopes in
`oauth_clients.scope`. Scopes the client did not register are silently
dropped, **without** raising an error to the caller. The minted token can
therefore omit `api:oemr` even when the agent-api requested it, with no
visible signal in the OAuth response.

**Symptom:** The agent-api believes it has `api:oemr`; OpenEMR's per-URL
scope check disagrees. Confusing because the scope appears in the request
and the token issues successfully.

### Gate 2 — Bearer validation

Standard bearer token validation in the OAuth resource-server middleware.
Fails with `401` if the token is missing, expired, or signature-invalid.
Not the cause of our `401` (we verified the token is valid via other API
calls).

### Gate 3 — Per-URL `api:oemr` scope gate

The legacy REST routes are gated on the bearer carrying the `api:oemr`
scope at the URL level. **Returns `403`, not `401`** — so when the bearer
carries no scopes (because gate 1 dropped them), the response is `401`
from gate 2's downstream behavior, not `403` from gate 3. This is why
"add `api:oemr` to the scope request" appears to do nothing: gate 1 silently
removes it before gate 3 ever sees it.

### Gate 4 — ACL `patients/docs` write|addonly

Even if the token carries `api:oemr` correctly, the route then performs:

```php
aclCheckCore("patients", "docs", $username, ['write', 'addonly'])
```

The password-grant user the agent-api uses does not carry this ACL by
default. **Returns `403`** if the user lacks the grant. This gate is
independent of OAuth scope.

### Mitigation (to retire the custom endpoint)

To re-activate the legacy REST fallback (which would render tier 3 redundant
and let the chain fall back to tier 2 naturally):

1. **Add `api:oemr` to the agent-api client's registered scopes** in the
   `oauth_clients.scope` column for the relevant `client_id`. This fixes
   gate 1 and lets gate 3 see the scope.
2. **Grant `patients/docs` `write|addonly` to the password-grant user's
   ACL.** This satisfies gate 4.

Both must be done together. After both are in place, tier 2 of the fallback
chain succeeds, tier 3 is no longer reached, and the custom endpoint can be
left in place dormant or removed entirely.

---

## 4.X Known false-positive on `no_phi_in_logs` cascade

The W2 eval runner (`agent-api/evals/runner.py`) attaches a capture handler
to the **root** logger at `DEBUG` so every first-party graph event is
available to the rubric layer. That decision has a side-effect: third-party
libraries pulled into the request path — `httpx`, `httpcore`,
`anthropic._base_client`, `langgraph`, `langchain` — emit DEBUG-level
records that include the **raw HTTP request and response bodies**. For a
clinical-document pipeline the request body is the source document text,
and the synthetic PHI used by the eval fixtures (`Marcus Webb`, `100847`,
`1962-03-14`, `Jane Doe`, ...) ends up in the captured set. The
`no_phi_in_logs` rubric then — correctly — flags those emissions and the
gate drops below its 1.00 floor (e.g. 0.84 on the
`regression/seed-strip-citations` regression branch run, where every lab
case fanned out an extractor vision call that left a debug body in the
capture).

This is a runner-side artifact, not a real leak in agent-api code: none of
our first-party `logger.*` call sites log raw clinical values (verified
against `agent-api/graph/`, `agent-api/extractors/`,
`agent-api/documents/`, and `agent-api/audit/`). The mitigation is to
suppress the third-party loggers for the lifetime of the capture: the
runner pins them to `WARNING` in `_attach_capture` and restores their
prior level in `_detach_capture`. The
`agent-api/tests/test_evals_no_phi_real_bug.py` regression test guards
that contract.

Tradeoff: if a future eval-time bug surfaces in `httpx` or `langgraph`,
its DEBUG diagnostic will not be in `captured_logs`. That is acceptable —
the rubric's contract is about agent-api emissions, not third-party
internals; an actual transport failure would surface as an exception or a
WARNING from those same loggers, both of which still flow through.

## 5. Cross-references

- `W2_ARCHITECTURE.md §4.2` — Path B (the documented upload path)
- `W2_ARCHITECTURE.md §4.2.1` — Custom upload path (deployment deviation)
- `W2_ARCHITECTURE.md §4.2.2` — Shared HMAC secret tradeoff
- `W2_ARCHITECTURE.md §4.2.4` — Custom FHIR Observation endpoint (parallel deviation)
- `W2_ARCHITECTURE.md §4.3` — Round-trip integrity (preserved)
- `W2_ARCHITECTURE.md §9.4` — Audit dual-target (preserved)
- `W2_ARCHITECTURE.md §12` — Risk Register entry #1 (the fallback chain)
- `agent-api/documents/fhir_writer.py` — fallback chain implementation
- `agent-api/auth/fhir_client.py` — `get_access_token` and `_token_cache_key`
- `interface/modules/custom_modules/oe-module-clinical-copilot/src/UploadController.php` — upload endpoint
- `interface/modules/custom_modules/oe-module-clinical-copilot/src/ObservationController.php` — Observation endpoint
- `interface/modules/custom_modules/oe-module-clinical-copilot/src/JwtMinter.php` — JWT shape reference
