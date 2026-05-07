# OAuth / SMART Notes

Notes for the dashboard port's OAuth2 / SMART-on-FHIR integration with the
local OpenEMR instance. **Secrets are not in this file** — they live in
`patient-dashboard/.env.local` (gitignored via repo `.gitignore`'s `.env.*`
rule; verified with `git check-ignore`).

## OpenEMR side — confidential client (registered 2026-05-07)

- **Registration method:** RFC 7591 dynamic client registration via
  `POST /oauth2/default/registration` (no admin UI required).
- **Approval:** OpenEMR's `ClientRepository::insertNewClient()` lands
  the new row in `oauth_clients.is_enabled = 0` when scopes contain
  `user/*`. Flipped to `1` directly in the docker MariaDB; this skips
  the admin-UI approval click.
- **Client name:** Patient Dashboard Port
- **Application type:** `private` (confidential, gets a client_secret)
- **Token endpoint auth method:** `client_secret_post`
- **Redirect URI:** `http://localhost:3000/api/auth/callback/openemr`
- **Post-logout redirect URI:** `http://localhost:3000/`
- **Scopes:**
  `openid fhirUser offline_access user/Patient.read
   user/AllergyIntolerance.read user/Condition.read
   user/MedicationRequest.read user/CareTeam.read user/Observation.read`

## Endpoints

- **Base URL:** `https://localhost:9300`
- **Authorize:** `https://localhost:9300/oauth2/default/authorize`
- **Token:** `https://localhost:9300/oauth2/default/token`
- **Logout:** `https://localhost:9300/oauth2/default/logout`
- **JWKS:** `https://localhost:9300/oauth2/default/jwk`
- **Discovery (RFC 8414):** `https://localhost:9300/oauth2/default/.well-known/openid-configuration`
- **SMART config:** `https://localhost:9300/oauth2/default/.well-known/smart-configuration`
- **FHIR base:** `https://localhost:9300/apis/default/fhir`

## OpenEMR globals (verified)

| Global | Value | Source |
|--------|-------|--------|
| `site_addr_oath` | `https://localhost:9300` | required for OAuth2 to function |
| `rest_api` | `1` | REST API enabled |
| `rest_fhir_api` | `1` | FHIR R4 API enabled |
| `oauth_app_manual_approval` | `0` | global flag — does not auto-approve `user/*` scopes |

## Quirks observed during 1.2 (Phase 1)

- **`/oauth2/default/.well-known/smart-configuration` returned 404 over
  HTTPS at first probe** — discovery endpoint may require a different
  path or may only publish after the first successful authorize. **Not
  yet investigated.** Token + authorize work without it. Revisit in 1.3
  if the auth library demands discovery.
- **Confidential vs public**: the registration endpoint requires
  `application_type=private` to mark a client as confidential (defaults
  to public, which then refuses `user/*` scopes with
  `invalid_client_metadata`). This is OpenEMR-specific; the standard
  RFC 7591 metadata field `token_endpoint_auth_method` alone was not
  enough.
- **Auto-approval gate**: even with `oauth_app_manual_approval=0` at
  the global level, scopes like `user/*` and `patient/*` still trigger
  `is_enabled=0` on insert. The SQL flip is the documented escape
  hatch; production deployments will go through Admin → API Clients.

## TODO during 1.3 (curl OAuth flow)

- [ ] Capture access-token response shape — token type, expires_in,
      scope echo-back, refresh_token presence
- [ ] Confirm `Patient/{uuid}` returns Gloria's record under
      `user/Patient.read`
- [ ] Confirm `Patient?identifier=4` (or a similar pid→uuid resolver)
      works — the dashboard receives a numeric pid from the URL but
      FHIR addresses by uuid
- [ ] Capture token-refresh behavior — record expires_in, refresh
      lifetime, idempotency
