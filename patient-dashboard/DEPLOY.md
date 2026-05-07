# Deployment runbook — patient-dashboard

Deployment of the migrated patient dashboard to Railway alongside the
existing OpenEMR service. R4.1 + R4.2 of the remaining-work plan.

## Prerequisites

- A Railway project that already runs OpenEMR.
- Railway CLI logged in (`railway login`) OR access to the Railway web UI.
- The deployed OpenEMR's globals: `oauth_password_grant=0` (production
  must use auth-code flow, not password grant).

## Architecture

```
                  ┌──────────────────┐
                  │  Railway project │
                  ├──────────────────┤
                  │  openemr         │  https://openemr.up.railway.app
                  │  (PHP, Apache)   │
                  ├──────────────────┤
                  │  patient-        │  https://patient-dashboard.up.railway.app
                  │  dashboard       │
                  │  (Next.js)       │
                  └──────────────────┘
```

The Next.js service is a separate Railway service in the same project.
Cross-service traffic uses the public URLs (Auth.js OAuth round trips
must hit the user's browser, so internal service URLs aren't usable for
the OAuth dance).

## R4.1 — Deploy Next.js to Railway

1. **Add a new Railway service** in the existing OpenEMR project:
   - Source: this GitHub repo
   - Root directory: `patient-dashboard/web`
   - Builder: Dockerfile (the included `Dockerfile` is a multi-stage
     Node 22 / Alpine build)
   - Replicas: 1

2. **Set environment variables** on the new service:
   - `NEXTAUTH_URL` — the public URL Railway assigns to this service
     (e.g. `https://patient-dashboard.up.railway.app`)
   - `NEXTAUTH_SECRET` — generate with `openssl rand -base64 32`
   - `AUTH_TRUST_HOST=true`
   - `OPENEMR_BASE_URL` — the public URL of the openemr service
     (e.g. `https://openemr.up.railway.app`)
   - `OPENEMR_OAUTH_CLIENT_ID` — populated in step R4.2 below
   - `OPENEMR_OAUTH_CLIENT_SECRET` — same
   - **DO NOT set `NODE_TLS_REJECT_UNAUTHORIZED=0`** in production —
     that's a dev-only escape hatch for the self-signed cert on local
     OpenEMR docker.

3. **Generate a public domain** for the service. Railway → Service →
   Settings → Networking → Generate Domain.

4. **Trigger a deploy** — push to the tracked branch or hit "Redeploy".
   Watch the build logs; the standalone Next.js bundle should produce
   in 2–3 minutes.

5. **Smoke-test the unauthenticated path**: open the public URL in
   incognito → expect a 307 to `/patient/4` → expect a 302 to
   `/login?callbackUrl=%2Fpatient%2F4` → expect the login page.

## R4.2 — OAuth client registration

The Next.js app uses Auth.js's authorization-code flow. OpenEMR must
have an OAuth client registered with the right redirect URI.

1. **Confirm `oauth_password_grant=0` on production OpenEMR.** Connect
   to the production database (Railway → openemr service → Data →
   Connect, or via mysql shell):

   ```sql
   SELECT gl_value FROM globals WHERE gl_name = 'oauth_password_grant';
   -- Expect: 0
   ```

   If it's not 0, set it:

   ```sql
   UPDATE globals SET gl_value = '0' WHERE gl_name = 'oauth_password_grant';
   ```

2. **Register a new OAuth client.** OpenEMR's admin Manage Modules UI
   for client registration is brittle; the SQL workaround is reliable.
   Compute a redirect URI:

   ```
   ${NEXTAUTH_URL}/api/auth/callback/openemr
   ```

   e.g. `https://patient-dashboard.up.railway.app/api/auth/callback/openemr`.

   Insert via SQL (substitute the redirect URI):

   ```sql
   -- Generate a client_id and a client_secret (40+ random chars each).
   -- Example using mysql random functions:
   SET @cid := SHA2(CONCAT(UUID(), RAND()), 256);
   SET @csec := SHA2(CONCAT(UUID(), RAND(), 'salt'), 512);

   INSERT INTO oauth_clients (
     client_id, client_secret, client_name, redirect_uri, grant_types,
     scope, user_id, is_confidential, is_enabled
   ) VALUES (
     @cid, @csec,
     'Patient Dashboard Port',
     'https://patient-dashboard.up.railway.app/api/auth/callback/openemr',
     'authorization_code refresh_token',
     'openid offline_access api:fhir user/Patient.read user/AllergyIntolerance.read user/Condition.read user/MedicationRequest.read user/CareTeam.read user/Observation.read',
     0, 1, 1
   );

   SELECT @cid AS client_id, @csec AS client_secret;
   ```

   Capture the printed `client_id` / `client_secret` — paste into the
   Next.js service's env vars `OPENEMR_OAUTH_CLIENT_ID` and
   `OPENEMR_OAUTH_CLIENT_SECRET`. Redeploy the Next.js service.

3. **End-to-end test**:
   - Open `https://patient-dashboard.up.railway.app` in incognito.
   - Expect redirects → land on the OpenEMR login page hosted by the
     openemr service.
   - Authenticate as `admin / pass` (or whatever the production
     credentials are).
   - Expect to bounce back to the dashboard with Gloria's data.

4. **Iframe path test** (for the OpenEMR module integration):
   - Log into OpenEMR directly.
   - Click the "Dashboard (Port)" tab.
   - Expect the iframe to load the same dashboard.
   - The first iframe load may trigger a fresh OAuth round trip —
     OpenEMR's session is independent from the Next.js / Auth.js
     session. Subsequent iframe loads reuse the Auth.js session cookie
     within the iframe origin.

## R4.2 readiness checklist

Before you call R4.2 done:

- [ ] `oauth_password_grant=0` in production OpenEMR globals
- [ ] OAuth client registered with the production redirect URI
- [ ] `OPENEMR_OAUTH_CLIENT_ID` / `_SECRET` set on Next.js service
- [ ] `NEXTAUTH_URL` set to the public Next.js URL on Next.js service
- [ ] `OPENEMR_BASE_URL` set to the public OpenEMR URL on Next.js service
- [ ] Login → dashboard → logout → login round-trip works in incognito
- [ ] Iframe path through the OpenEMR module also works (same flow)
- [ ] Token refresh works (let one expire, or shorten TTL temporarily)

## Out of scope

- Custom domain on either service (Railway-assigned URLs are sufficient)
- HSTS / cert pinning (Railway's edge handles TLS)
- Horizontal scaling (single replica is enough for the demo)
