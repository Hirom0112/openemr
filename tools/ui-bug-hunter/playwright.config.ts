import { defineConfig } from '@playwright/test';

/**
 * Playwright test harness for the OpenEMR Clinical Co-Pilot UI.
 *
 * This config sits alongside the autonomous Claude-Agent-SDK hunter in
 * src/hunt.ts. The autonomous agent explores; this harness enforces
 * deterministic regressions for the bugs already identified.
 *
 * Two projects share storageState produced by global-setup.ts:
 *   - admin: backend / power-user views
 *   - sara : the clinician persona that drives the Co-Pilot panel
 */
export default defineConfig({
  testDir: './specs',
  timeout: 60_000,
  workers: 1, // OpenEMR session is server-bound; parallel runs corrupt $_SESSION
  reporter: [['list'], ['html', { open: 'never' }]],
  globalSetup: './global-setup.ts',
  use: {
    baseURL: process.env.TARGET_URL || 'http://localhost:8300',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'admin',
      use: { storageState: '.auth/admin.json' },
    },
    {
      name: 'sara',
      use: { storageState: '.auth/sara.json' },
    },
  ],
});
