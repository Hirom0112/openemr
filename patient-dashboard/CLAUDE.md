# Patient Dashboard Migration — Project Rules

You are helping port the OpenEMR patient dashboard to a modern framework.

## Non-negotiables

1. The brief is reimplementation, not redesign. Feature parity with the
   existing OpenEMR patient dashboard is the standard. Visual departure
   from the original requires explicit justification in the migration doc.

2. The graded artifact is `PATIENT_DASHBOARD_MIGRATION.md`, especially
   the framework defense section. That section is owned by the human.
   Do not modify it without explicit approval. You may critique it on
   request.

3. Always read `dashboard-inventory.md` before building any UI. Always
   read `dashboard-api-map.md` before making any API call. These two
   files are the spec.

4. Slice your work. One file or one bounded change per response. State
   acceptance criteria as observable outcomes ("the header renders the
   same fields as reference-screenshots/header.png"). State the
   verification step the human will run. Do not queue parallel work
   until the pattern has been verified once.

5. Do not invent endpoints, fields, or behaviors. If something isn't
   in the inventory or the API map, ask before adding it.

6. This is a separate project from the Clinical Co-Pilot under
   `agent-api/`. Do not import from or depend on Co-Pilot code. The
   two projects share an OpenEMR backend but share nothing else.

7. Before any architectural work, ground yourself: read this file,
   the inventory, the API map, and the migration doc. Then proceed.