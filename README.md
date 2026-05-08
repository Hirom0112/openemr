# Clinical Co-Pilot

> **Grading this submission?** Start at [`SUBMISSION.md`](./SUBMISSION.md). It's a single page with live URLs, eval-gate evidence, deployed-vs-spec table, and the honest deferral list.

## Quick links for graders

| What you want | Where to find it |
| --- | --- |
| **Single-page summary** | [`SUBMISSION.md`](./SUBMISSION.md) |
| **Live deployed app (OpenEMR)** | <https://clinical-copilot-openemr-production.up.railway.app> — login `admin` / `pass` |
| **Live deployed agent-api** | <https://copilot-agent-api-production.up.railway.app/health> |
| **Eval-gate evidence (regression PR fails the 156-case suite)** | <https://github.com/Hirom0112/openemr/pull/1> |
| **Architecture (W2)** | [`W2_ARCHITECTURE.md`](./W2_ARCHITECTURE.md) — supervisor, workers, RAG, observability |
| **Architecture (W1)** | [`W1_ARCHITECTURE.md`](./W1_ARCHITECTURE.md) — dispatcher, tools, verification |
| **Cost & latency report** | [`COST_LATENCY_REPORT.md`](./COST_LATENCY_REPORT.md) |
| **Eval suite docs** | [`agent-api/evals/README.md`](./agent-api/evals/README.md) — 156 cases, 14 rubrics, CI gate |
| **Security tradeoffs** | [`docs/SECURITY_TRADEOFFS.md`](./docs/SECURITY_TRADEOFFS.md) — FHIR Binary deviation + four-gate investigation |
| **Local setup** | [Setup section below](#setup) |

The deployed-build deviation (custom JWT-protected upload because the upstream OpenEMR build returns `404` on FHIR `Binary` POST and `401` on legacy REST upload) is documented in [`W2_ARCHITECTURE.md` §4.2.1 / §4.2.2](./W2_ARCHITECTURE.md) and [`docs/SECURITY_TRADEOFFS.md`](./docs/SECURITY_TRADEOFFS.md). It is reversible.

---

A Gauntlet AI project building a Clinical Co-Pilot agent on top of OpenEMR.

**Status:** agent-api (FastAPI) and agent-ui (React) run alongside OpenEMR via `docker/development-easy/docker-compose.copilot.yml`.

## Week 1 vs Week 2 Capabilities

The deliverable ships in two additive passes. Week 1 (W1) is the structured-data triage assistant; Week 2 (W2) layers in document ingestion, a multi-agent supervisor graph, hybrid retrieval over a guideline corpus, and a wrong-patient critic. Reading the table below should make it unambiguous which feature lives in which deliverable.

| Capability | Week 1 (shipped) | Week 2 (added) |
|---|---|---|
| Triage census + per-patient briefing | Census ranked by triage signals; concise per-patient brief assembled from FHIR | unchanged |
| Medication safety + targeted query + handoff | Active-medication safety screen, targeted FHIR query tools, structured nurse-handoff packet | unchanged |
| Document ingestion | — | `POST /document/ingest`: OCR + Claude vision extraction, FHIR `DocumentReference` round-trip, idempotent claim on `(doc_ref_id, content_sha256)` |
| Multi-agent supervisor graph | Single dispatcher with deterministic fast-path | LangGraph supervisor + 4 workers + critic + finalize, exposed via `POST /agent/w2/dispatch` (SSE) |
| Hybrid RAG over guideline corpus | — | pgvector dense + tsvector sparse with Cohere rerank and merged-top-N fallback, `POST /evidence/search` |
| Wrong-patient detection | Patient-id normalization and census-scope guard at dispatcher entry | MRN-dominant demographic comparator (W2_ARCHITECTURE §5.6) emitting `agent_w2_demographic_checks_total` |
| Critic with citation fidelity | W1 verification layer preserved on the structured-data path | New critic with schema / citation / fidelity / demographic checks, surfacing `pass` / `soft_warn` / `hard_block` |
| Bbox overlay UI | Chat surface, brief panel | `agent-ui` `DocumentViewer` + `BboxOverlay` + `CitationChip` for evidence-grounded chips |
| Eval gate | W1 dispatcher tests under the same pytest gate (`hard_failure` / `clinical_accuracy` markers); see [`W1_ARCHITECTURE.md §6.1`](./W1_ARCHITECTURE.md) | 156-case W2 golden set + GH Actions `w2-eval` job in `.github/workflows/copilot-eval.yml` with `baseline.json`, `diff_baseline.py`, advisory pre-push hook |

Week 1 features are unchanged in Week 2 by architectural constraint #3 (additive, not a rewrite): the W2 graph and document path live behind their own routes, share observability primitives with W1, and never modify W1 code paths or contracts.

On the deployed pilot, documents currently round-trip via a custom JWT-protected upload endpoint inside the existing `oe-module-clinical-copilot` module rather than the FHIR `Binary` POST or legacy REST upload, because both upstream paths are unavailable on the OpenEMR build we deploy (FHIR `Binary` advertises `read` only and returns `404` on POST; legacy REST is gated by an ACL the password-grant user does not carry). Documents still land in OpenEMR's own `documents` table and the FHIR `DocumentReference` read path still surfaces them — see [docs/SECURITY_TRADEOFFS.md](docs/SECURITY_TRADEOFFS.md) for the full deviation summary, secret-handling, and the four-gate REST investigation. The `W2_ARCHITECTURE.md` §4.2.1 + §4.2.2 sections describe the deviation and tradeoffs. The custom path is reversible — when OpenEMR's FHIR Binary write or legacy REST upload becomes available, the chain naturally falls back through them first.

## Prerequisites

- Git
- Docker Desktop (running)
- ~15 GB free disk space

> **Apple Silicon (M-series Mac):** The instructions below use `docker/development-easy`, which runs multi-arch images transparently via Docker Desktop. No separate ARM variant is needed. Intel Mac, Linux, and Windows users follow the same steps.

## Setup

1. Fork [openemr/openemr](https://github.com/openemr/openemr) on GitHub.

2. Clone your fork and enter the repo:
   ```sh
   git clone https://github.com/<your-username>/openemr.git
   cd openemr
   ```

3. Add the upstream remote:
   ```sh
   git remote add upstream https://github.com/openemr/openemr.git
   ```

4. Create and check out the working branch:
   ```sh
   git checkout -b clinical-copilot
   ```

5. Start the Docker stack:
   ```sh
   cd docker/development-easy
   docker compose up -d
   ```

6. Wait for initialization. Tail the logs until `/meta/health/readyz` returns 200:
   ```sh
   docker compose logs -f openemr
   ```

7. Open http://localhost:8300 and log in with `admin` / `pass`.

8. Load demo patient data:
   ```sh
   docker compose exec openemr /root/devtools dev-reset-install-demodata
   ```

9. Log back in and verify demo patients are present (patient list populated, charts contain demographics, encounters, medications, problems, and lab data).

## Access

| Service    | URL                       | Credentials          |
|------------|---------------------------|----------------------|
| OpenEMR    | http://localhost:8300      | admin / pass         |
| phpMyAdmin | http://localhost:8310      | openemr / openemr    |

## Stopping and resetting

```sh
# Stop containers, preserve data volumes
docker compose down

# Stop containers and wipe volumes for a clean start
docker compose down -v
```

## Known warnings (benign)

The following warnings appear in logs in the dev environment and are expected — not bugs:

- Apache `ServerName` not set
- Self-signed SSL certificate warnings (`AH01906`, `AH01909`)

## Deployed

- **OpenEMR (Railway):** https://clinical-copilot-openemr-production.up.railway.app/interface/login/login.php?site=default
- **Login:** `admin` / `pass`
- **Branch:** `clinical-copilot`

---

[![Syntax Status](https://github.com/openemr/openemr/actions/workflows/syntax.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/syntax.yml)
[![Styling Status](https://github.com/openemr/openemr/actions/workflows/styling.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/styling.yml)
[![Testing Status](https://github.com/openemr/openemr/actions/workflows/test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/test.yml)
[![JS Unit Testing Status](https://github.com/openemr/openemr/actions/workflows/js-test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/js-test.yml)
[![PHPStan](https://github.com/openemr/openemr/actions/workflows/phpstan.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/phpstan.yml)
[![Rector](https://github.com/openemr/openemr/actions/workflows/rector.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/rector.yml)
[![ShellCheck](https://github.com/openemr/openemr/actions/workflows/shellcheck.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/shellcheck.yml)
[![Docker Compose Linting](https://github.com/openemr/openemr/actions/workflows/docker-compose-lint.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/docker-compose-lint.yml)
[![Dockerfile Linting](https://github.com/openemr/openemr/actions/workflows/hadolint.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/hadolint.yml)
[![Isolated Tests](https://github.com/openemr/openemr/actions/workflows/isolated-tests.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/isolated-tests.yml)
[![Inferno Certification Test](https://github.com/openemr/openemr/actions/workflows/inferno-test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/inferno-test.yml)
[![Composer Checks](https://github.com/openemr/openemr/actions/workflows/composer.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/composer.yml)
[![Composer Require Checker](https://github.com/openemr/openemr/actions/workflows/composer-require-checker.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/composer-require-checker.yml)
[![API Docs Freshness Checks](https://github.com/openemr/openemr/actions/workflows/api-docs.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/api-docs.yml)
[![codecov](https://codecov.io/gh/openemr/openemr/graph/badge.svg?token=7Eu3U1Ozdq)](https://codecov.io/gh/openemr/openemr)

[![Backers on Open Collective](https://opencollective.com/openemr/backers/badge.svg)](#backers) [![Sponsors on Open Collective](https://opencollective.com/openemr/sponsors/badge.svg)](#sponsors)

# OpenEMR

[OpenEMR](https://open-emr.org) is a Free and Open Source electronic health records and medical practice management application. It features fully integrated electronic health records, practice management, scheduling, electronic billing, internationalization, free support, a vibrant community, and a whole lot more. It runs on Windows, Linux, Mac OS X, and many other platforms.

### Contributing

OpenEMR is a leader in healthcare open source software and comprises a large and diverse community of software developers, medical providers and educators with a very healthy mix of both volunteers and professionals. [Join us and learn how to start contributing today!](https://open-emr.org/wiki/index.php/FAQ#How_do_I_begin_to_volunteer_for_the_OpenEMR_project.3F)

> Already comfortable with git? Check out [CONTRIBUTING.md](CONTRIBUTING.md) for quick setup instructions and requirements for contributing to OpenEMR by resolving a bug or adding an awesome feature 😊.

### Support

Community and Professional support can be found [here](https://open-emr.org/wiki/index.php/OpenEMR_Support_Guide).

Extensive documentation and forums can be found on the [OpenEMR website](https://open-emr.org) that can help you to become more familiar about the project 📖.

### Reporting Issues and Bugs

Report these on the [Issue Tracker](https://github.com/openemr/openemr/issues). If you are unsure if it is an issue/bug, then always feel free to use the [Forum](https://community.open-emr.org/) and [Chat](https://www.open-emr.org/chat/) to discuss about the issue 🪲.

### Reporting Security Vulnerabilities

Check out [SECURITY.md](.github/SECURITY.md)

### API

Check out [API_README.md](API_README.md)

### Docker

Check out [DOCKER_README.md](DOCKER_README.md)

### FHIR

Check out [FHIR_README.md](FHIR_README.md)

### For Developers

If using OpenEMR directly from the code repository, then the following commands will build OpenEMR (Node.js version 24.* is required) :

```shell
composer install --no-dev
npm install
npm run build
composer dump-autoload -o
```

### Contributors

This project exists thanks to all the people who have contributed. [[Contribute]](CONTRIBUTING.md).
<a href="https://github.com/openemr/openemr/graphs/contributors"><img src="https://opencollective.com/openemr/contributors.svg?width=890" /></a>


### Sponsors

Thanks to our [ONC Certification Major Sponsors](https://www.open-emr.org/wiki/index.php/OpenEMR_Certification_Stage_III_Meaningful_Use#Major_sponsors)!


### License

[GNU GPL](LICENSE)
