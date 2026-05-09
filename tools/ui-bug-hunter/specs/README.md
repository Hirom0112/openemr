# UI bug-hunter — Playwright verification harness

This is the deterministic-regression counterpart to the autonomous Claude-agent
hunter that lives in `../src/hunt.ts`. The autonomous agent explores the UI and
finds new issues; the specs in this folder pin down the bugs we already know
about so they don't regress.

The two harnesses share the same package and `node_modules/`, but are otherwise
independent. The agent does **not** need `@playwright/test`, and the specs do
**not** need the Claude Agent SDK or `ANTHROPIC_API_KEY`.

## Prerequisites

- The OpenEMR dev stack running on `http://localhost:8300` (override with
  `TARGET_URL`). Bring it up with:
  ```bash
  cd docker/development-easy
  docker compose up --detach --wait
  ```
- Node 20+ and npm.
- Browser binaries installed (one-time):
  ```bash
  npx playwright install chromium
  ```

## Running

From `tools/ui-bug-hunter/`:

```bash
npm install                       # install @playwright/test + agent deps
npx playwright install chromium   # one-time browser download
npm test                          # headless run
npm run test:headed               # watch the browser drive itself
npm run test:ui                   # interactive Playwright UI mode
```

The HTML report lands in `playwright-report/`. Auth state for both personas is
cached in `.auth/{admin,sara}.json` (gitignored) by `global-setup.ts`.

## Credentials

`global-setup.ts` logs in as both `admin` and `sara` and persists cookies to
`.auth/`. Admin password is `pass`; sara's demo password is `chen`.

If sara's password isn't `chen` (e.g. on a non-default deploy), set the env var:

```bash
SARA_PASSWORD=correct-horse npm test
```

The setup throws a clear error if both attempts fail — you'll see it in the
test runner output before any spec executes.

## Layout

| File | Purpose |
| --- | --- |
| `_helpers.ts` | Shared `openCopilot()` + `waitForCensus()` helpers; canonical iframe selector. |
| `greeting.spec.ts` | Greeting renders personalized (no `{User}` placeholder leak). |
| `census-stability.spec.ts` | Patient count is stable across reloads. **Currently `test.fixme`** — flip on after Issue 2. |
| `theme.spec.ts` | All tool-output containers share a small canonical token set. **Currently `test.fixme`** — flip on after Issue 1. |
| `chart-button.spec.ts` | "View in Chart" popups have integer pid and a populated body. |
| `latency.spec.ts` | Soft probe of `/agent`, `/triage`, `/briefing` p95/mean/stdev. Never fails on latency yet. |
| `chat-behaviors.spec.ts` | Chat-surface behaviors that the prompt-eval suite cannot test: chart-button gating, auto-collapse with census exemption, census Refresh updating the timestamp, handoff cascade ≥300ms stagger, markdown-narrative rendering. |

## Selectors

The Co-Pilot iframe is located via `iframe[src*="oe-module-clinical-copilot"]`.
Inside the iframe, specs use role/text selectors only. We deliberately do
**not** rely on `data-testid` yet because Issue 1 is rewriting the panel's DOM
in parallel and we don't want to seed selectors that are about to be deleted.
After Issue 1 lands, add stable `data-testid` attributes to the
`agent-ui/src/components/` renderers and migrate this harness to use them.

## Adding a new spec

1. Drop a file in `specs/` ending in `.spec.ts`.
2. If it needs Sara's session, add `test.use({ storageState: '.auth/sara.json' })`
   at the describe level. (The default project also matches; explicit is better.)
3. Use `openCopilot(page)` / `waitForCensus(frame)` from `_helpers.ts` instead
   of re-implementing the login + tab-click + iframe dance.
