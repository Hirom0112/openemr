# oe-module-patient-dashboard-port

Thin OpenEMR custom module that hosts the modernized Next.js patient
dashboard inside OpenEMR's chrome.

## What this module does

1. Validates the OpenEMR session (`AclMain::aclCheckCore('patients', 'med')`).
2. Resolves the active patient pid from `$_SESSION` (legacy or
   `$_SESSION['OpenEMR']` nested) — falls back to pid 4 (Gloria) when
   no active patient is set, so the tab is always demonstrable.
3. Renders a full-bleed iframe pointing at the Next.js dashboard's
   `/patient/{pid}` route.
4. Adds a "Dashboard (Port)" entry to the OpenEMR top navigation via
   `MenuEvent::MENU_UPDATE`.

The module owns no data fetching and no business logic — the Next.js
app handles its own OAuth round trip with OpenEMR for FHIR access.

## Configuration

The dashboard URL is resolved in this order:

| Source | Use when |
|---|---|
| `PATIENT_DASHBOARD_URL` env var | Production (Railway) |
| `$GLOBALS['patient_dashboard_url']` | Per-instance override |
| `http://localhost:3000` | Dev (when host is localhost or `PATIENT_DASHBOARD_DEV_MODE=1`) |

When none resolves and the request is non-local, the iframe is
suppressed and a red banner explains the misconfiguration. The tab
never silently shows a blank or broken page.

## Install

1. Copy this directory under
   `interface/modules/custom_modules/oe-module-patient-dashboard-port/`.
2. In OpenEMR admin → Modules → Manage Modules, click **Register** then
   **Install** then **Enable** for "Patient Dashboard (Port)".
3. (Production) Set `PATIENT_DASHBOARD_URL` on the OpenEMR container to
   the public Next.js URL.
4. Reload OpenEMR; the new tab appears in the top navigation.

If the admin Manage Modules UI is brittle on the deployed instance,
you can flip the `modules` row directly:

```sql
UPDATE modules
   SET mod_active = 1, mod_enabled = 1
 WHERE mod_directory = 'oe-module-patient-dashboard-port';
```

## Files

| File | Purpose |
|---|---|
| `module.php` | OpenEMR module-registry metadata |
| `openemr.bootstrap.php` | Subscribes the Bootstrap class to OpenEMR events |
| `src/Bootstrap.php` | Adds the menu tab via `MenuEvent::MENU_UPDATE` |
| `index.php` | Entry: auth check + iframe to `${PATIENT_DASHBOARD_URL}/patient/{pid}` |
