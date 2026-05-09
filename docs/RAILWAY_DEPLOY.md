# Railway deployment — Clinical Co-Pilot

This file lists the environment variables required to run the Clinical
Co-Pilot panel against a Railway-deployed OpenEMR + agent-api stack, plus
the one-shot seed command.

## Required env vars on the OpenEMR service

| Variable | Required | Notes |
| --- | --- | --- |
| `COPILOT_AGENT_API_URL` | yes (production) | **Public** Railway URL of the `copilot-agent-api` service, e.g. `https://copilot-agent-api-production.up.railway.app`. **Not** the internal `*.railway.internal` URL — the browser fetches this directly, so it has to be reachable from the user's network. If unset on a deployed container, the panel renders a visible misconfiguration banner instead of silently fetching `http://localhost:8400`. |
| `COPILOT_DEV_MODE` | no | Set to `1` only if you intentionally want the localhost fallback on a deployed container (rare; mostly for compose-style local dev where `HTTP_HOST` does not contain `localhost`). |

## Required env vars for the seed script

`scripts/seed-railway.sh` reads:

| Variable | Default | Notes |
| --- | --- | --- |
| `OE_USER` | `sara` | OpenEMR clinician username. The loader writes this user's `users.id` into `form_encounter.provider_id` so the census panel filters to their patients. Use `sara` if Sara Chen is your demo clinician. |
| `OE_PASS` | — | Required. |
| `BASE_URL` | — | Public OpenEMR URL (https://...). |
| `CLIENT_ID` | — | OAuth2 client ID for the loader (created via `scripts/01-register-fhir-client.sh`). |
| `CLIENT_SECRET` | — | OAuth2 client secret. |
| `MYSQL_HOST` | — | Railway MySQL TCP-proxy host. |
| `MYSQL_PORT` | `3306` | |
| `MYSQL_USER` | `root` | |
| `MYSQL_PASS` | — | Required. |
| `MYSQL_DB` | `openemr` | |

## One-shot seed command

```bash
railway run -s clinical-copilot-openemr -- ./scripts/seed-railway.sh
```

The script is idempotent: it probes `patient_data` for the canonical first
synthetic patient (Marcus Webb, DOB 1968-03-14) and exits 0 if the seed has
already run. Re-running is safe and cheap.

## What still needs manual attention

1. Set `COPILOT_AGENT_API_URL` on the OpenEMR Railway service.
2. Trigger a redeploy of the OpenEMR service so the new env var is applied.
3. Run `scripts/seed-railway.sh` once. Subsequent deploys do not need it.

## Probing the deployed MySQL

`railway run -s <service> -- <cmd>` runs locally with env injected, so `MYSQL_HOST=mysql.railway.internal` won't resolve from your laptop. Use `railway ssh --service clinical-copilot-openemr '<cmd>'` to execute inside the container instead — that's how to run ad-hoc `mysql`/`php` probes against the deployed DB.
