# Clinical Co-Pilot — Railway Deployment Guide

## Overview

This document is the reference for deploying the OpenEMR fork and Clinical Co-Pilot agent to Railway. Stage 2 covers OpenEMR only. Agent services (Stage 3+) are added to the same Railway project when the agent code is built.

---

## Stage 2 — OpenEMR on Railway

### What Gets Created

Three files are added to the repo for Railway deployment:

```
Dockerfile                          ← builds the OpenEMR image from this fork
railway.toml                        ← Railway project configuration
docker/railway/apache-proxy.conf    ← Apache snippet for HTTPS detection behind Railway's proxy
```

These do not affect local development. The existing `docker/development-easy/` setup is unchanged.

---

### Why Each File Exists

**`docker/railway/apache-proxy.conf`**

Railway terminates TLS at its reverse proxy and forwards plain HTTP to the container with `X-Forwarded-Proto: https`. OpenEMR detects HTTPS via `$_SERVER['HTTPS']` and `$_SERVER['REQUEST_SCHEME']`, which Apache sets based on the local connection — not the forwarded header. Without this fix, OpenEMR generates HTTP URLs, session cookies lack the `Secure` flag, and redirect loops may occur.

This snippet uses `mod_setenvif` (loaded by default in Alpine Apache — no `LoadModule` needed) to translate the forwarded header into what OpenEMR expects:

```apache
SetEnvIf X-Forwarded-Proto "https" HTTPS=on
SetEnvIf X-Forwarded-Proto "https" REQUEST_SCHEME=https
Header always set Strict-Transport-Security "max-age=31536000; includeSubDomains" env=HTTPS
```

Note: `REMOTE_ADDR` will show Railway's internal proxy IP, not the real client IP. Acceptable for this deployment.

**`Dockerfile`**

The flex image (`openemr/openemr:flex`) is the correct base. It carries compiled vendor assets and node_modules that the repo itself does not include (they are gitignored). Starting from `openemr/openemr:latest` would require re-running composer install and npm build inside the image.

The Dockerfile injects the Apache proxy config, then layers the fork's code over the base image. The `sites/` directory is a runtime Railway volume — it is not overwritten by the `COPY` at container start.

**`railway.toml`**

Defines the build method and restart policy. The healthcheck is omitted for first deploy — see First Deploy note below.

---

### Files to Create

#### `docker/railway/apache-proxy.conf`

```apache
# Translate Railway's X-Forwarded-Proto into Apache/PHP HTTPS detection variables.
# Uses mod_setenvif — loaded by default in Alpine Apache, no LoadModule needed.
SetEnvIf X-Forwarded-Proto "https" HTTPS=on
SetEnvIf X-Forwarded-Proto "https" REQUEST_SCHEME=https
Header always set Strict-Transport-Security "max-age=31536000; includeSubDomains" env=HTTPS
```

#### `Dockerfile`

```dockerfile
FROM openemr/openemr:flex

# Inject proxy trust config before Apache starts
COPY docker/railway/apache-proxy.conf /etc/apache2/conf.d/railway-proxy.conf

# Layer fork code over the base image.
# vendor/, node_modules/, and compiled assets from the base image are preserved.
# sites/ is a runtime Railway volume — not overwritten by COPY at container start.
COPY --chown=apache:apache . /var/www/localhost/htdocs/openemr/

# Remove dev and build artifacts that don't belong in the deployed image
RUN rm -rf /var/www/localhost/htdocs/openemr/docker \
           /var/www/localhost/htdocs/openemr/.git \
           /var/www/localhost/htdocs/openemr/node_modules
```

#### `railway.toml`

```toml
[build]
builder = "DOCKERFILE"
dockerfilePath = "Dockerfile"

[deploy]
# healthcheckPath is intentionally omitted for the first deploy.
# First-boot database initialization can exceed any reasonable timeout.
# Add "/meta/health/readyz" after confirming the first boot completes successfully.
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 3
```

---

### Railway Dashboard Setup

#### 1. Create the project

- New project → Deploy from GitHub repo
- Select this repo, branch: `clinical-copilot`
- Railway auto-detects the `Dockerfile` at root

#### 2. Add MySQL plugin

- In the Railway project: New service → Database → MySQL
- Railway provisions a managed MySQL instance and exposes reference variables

#### 3. Configure persistent volume (openemr service)

Add a volume to the openemr service:

| Mount Path | Size |
|---|---|
| `/var/www/localhost/htdocs/openemr/sites` | 5 GB |

This path contains `sites/default/sqlconf.php` — the install sentinel. Without it, every redeploy triggers a fresh install attempt. It also holds all uploaded documents, site config, EDI files, and portal documents.

#### 4. Set environment variables (openemr service)

```
MYSQL_HOST=${{MySQL.MYSQL_HOST}}
MYSQL_ROOT_PASS=${{MySQL.MYSQL_ROOT_PASSWORD}}
MYSQL_USER=openemr
MYSQL_PASS=<choose a real password>
OE_USER=admin
OE_PASS=<choose a real password — not "pass">
OPENEMR_DOCKER_ENV_TAG=railway-deploy
```

Do NOT set `EASY_DEV_MODE`, `EASY_DEV_MODE_NEW`, or `DEVELOPER_TOOLS`. These are development-only flags that alter entrypoint behavior.

---

### First Deploy Notes

**Healthcheck:** The `railway.toml` intentionally omits `healthcheckPath`. On first boot, OpenEMR runs full database initialization before it can serve `/meta/health/readyz`. Railway marking a deploy as failed during the bootstrap window is a false failure. Watch the build/deploy logs directly. When the install completes and the login page loads, add the healthcheck in a follow-up commit:

```toml
[deploy]
healthcheckPath = "/meta/health/readyz"
healthcheckTimeout = 300
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 3
```

**Second boot behavior:** Confirmed from `openemr.sh` source (openemr/openemr-devops, `docker/openemr/flex/openemr.sh`). The sentinel is `/var/www/localhost/htdocs/openemr/sites/default/sqlconf.php`. When this file exists with `$config = 1`, the entire setup block is skipped — no migrations re-run, no re-initialization. Container restarts and redeployments are safe as long as the sites volume persists.

---

### Verification Checklist

Run in order. Each item must pass before proceeding to the next.

- [ ] `https://<project>.up.railway.app/` loads the OpenEMR login screen over HTTPS (no certificate warning)
- [ ] No HTTP→HTTPS redirect loop (proxy config is working — `SetEnvIf` snippet applied correctly)
- [ ] Login with `OE_USER` / `OE_PASS` credentials works
- [ ] `/apis/default/fhir/Patient` returns **401 or 403**, not 500 (FHIR auth layer is functional, not crashing)
- [ ] FHIR connector admin page loads at `/interface/smart/register-app.php`
- [ ] Upload a document via OpenEMR UI, trigger a Railway redeploy, confirm the document is still present (sites volume persistence verified end-to-end)
- [ ] Trigger a second redeploy — confirm setup is skipped (second boot sentinel working)
- [ ] After all above pass: add `healthcheckPath = "/meta/health/readyz"` to `railway.toml` and redeploy — confirm deploy succeeds with healthcheck enabled

---

## Stage 3+ — Agent Services on the Same Railway Project

Agent services are added to the existing Railway project when the code exists. Do not add them before the code is built.

| Service | When | Source |
|---|---|---|
| redis | Stage 3 | Railway Redis plugin — managed |
| agent-api | Stage 3 | `agent-api/Dockerfile` |
| monitoring-daemon | Stage 3 | `agent-monitoring/Dockerfile` |
| grafana | Stage 3 | Docker image |
| prometheus | Stage 3 | Docker image |
| Langfuse | Stage 3 | Langfuse Cloud free tier — no Railway service needed |

**Langfuse:** Use the Cloud free tier. The scrubbed event stream (ARCHITECTURE.md §5.2) contains no PHI — only operational metadata with hashed IDs. No BAA required. Eliminates a self-hosted service and its PostgreSQL dependency from the Railway project.

---

## Local Development

Unchanged. Use `docker/development-easy/` as documented in the main README and CONTRIBUTING.md. The `Dockerfile` and `railway.toml` at the repo root are Railway-only and do not affect the local dev environment.
