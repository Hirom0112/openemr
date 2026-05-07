# Patient Dashboard — Web

The OpenEMR patient dashboard, ported to a modern stack. This is a separate
project from the Clinical Co-Pilot under `../agent-api/`.

## Stack

- **Next.js 15+ (App Router)** — server components keep the FHIR token off the browser
- **TypeScript** — strict mode
- **Auth.js (NextAuth v5 beta)** — OAuth2 against OpenEMR
- **shadcn/ui + Tailwind CSS** — clinical-density UI primitives
- **React 19**

## Run

```bash
npm install
npm run dev          # dev server on http://localhost:3000
npm run build        # production build
npm run start        # serve the production build
npm run lint         # eslint
```

Smoke test: visit http://localhost:3000/patient/4 — it renders a placeholder
that the seven-card dashboard will replace in Phase 4.x.

## Environment

Copy `.env.example` to `.env.local` and fill in values:

```bash
cp .env.example .env.local
```

`.env.local` is gitignored. The actual OAuth client + secret are populated
in **Phase 3.2** of the migration. The FHIR client lands in **Phase 3.3**.

## Layout

```
src/
  app/
    patient/[id]/page.tsx   # patient dashboard route (placeholder for now)
  components/
    cards/                  # the seven dashboard cards (Phase 4.x)
    ui/                     # shadcn primitives
  lib/
    auth/                   # Auth.js config (Phase 3.2)
    fhir/                   # typed FHIR client (Phase 3.3)
```

## See also

- `../PATIENT_DASHBOARD_MIGRATION.md` — graded artifact, framework defense
- `../dashboard-inventory.md` — UI spec (read before building any card)
- `../dashboard-api-map.md` — API spec (read before any FHIR call)
- `./AGENTS.md` — Next.js version-specific guidance for AI agents
