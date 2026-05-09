import { chromium, type FullConfig } from '@playwright/test';
import { mkdirSync, existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// ESM equivalent of __dirname.
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

/**
 * Logs in as both `admin` and `sara`, persists each session to .auth/*.json
 * so test specs can pick up an authenticated browser context via storageState.
 *
 * OpenEMR's login form (templates/login/partials/input/{username,password}.html.twig):
 *   - input[name="authUser"]
 *   - input[name="clearPass"]
 *   - submits to /interface/login/login.php?site=default
 *   - on success the browser lands on /interface/main/tabs/main.php
 *
 * Sara's demo password is `chen`. We try that first and fall back to the
 * SARA_PASSWORD env var if that fails (e.g. on a non-default deploy). If both
 * fail we throw with a clear message — the rest of the suite cannot run
 * without sara's session.
 */

const BASE_URL = process.env.TARGET_URL || 'http://localhost:8300';
const AUTH_DIR = resolve(__dirname, '.auth');

interface Credentials {
  user: string;
  primaryPass: string;
  fallbackPass?: string;
  storagePath: string;
}

async function saveSession(creds: Credentials): Promise<void> {
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext();
    const page = await context.newPage();

    const tryWith = async (password: string): Promise<boolean> => {
      await page.goto(`${BASE_URL}/interface/login/login.php?site=default`, {
        waitUntil: 'domcontentloaded',
      });
      await page.fill('input[name="authUser"]', creds.user);
      await page.fill('input[name="clearPass"]', password);
      await Promise.all([
        page.waitForLoadState('networkidle').catch(() => undefined),
        page.click('button[type="submit"], input[type="submit"]'),
      ]);
      const url = page.url();
      // Successful login navigates away from login.php (typically to main/tabs/main.php).
      return !url.includes('/interface/login/login.php');
    };

    let ok = await tryWith(creds.primaryPass);
    if (!ok && creds.fallbackPass) {
      ok = await tryWith(creds.fallbackPass);
    }
    if (!ok) {
      throw new Error(
        `[ui-bug-hunter] login failed for user "${creds.user}". ` +
          `Tried "${creds.primaryPass}"` +
          (creds.fallbackPass ? ` and SARA_PASSWORD fallback` : '') +
          `. Set SARA_PASSWORD in your environment if sara's password is not "chen", ` +
          `e.g. \`SARA_PASSWORD=correct-horse npm test\`.`,
      );
    }

    await context.storageState({ path: creds.storagePath });
    await context.close();
  } finally {
    await browser.close();
  }
}

export default async function globalSetup(_config: FullConfig): Promise<void> {
  if (!existsSync(AUTH_DIR)) {
    mkdirSync(AUTH_DIR, { recursive: true });
  }

  await saveSession({
    user: 'admin',
    primaryPass: 'pass',
    storagePath: resolve(AUTH_DIR, 'admin.json'),
  });

  const saraEnv = process.env.SARA_PASSWORD;
  await saveSession({
    user: 'sara',
    primaryPass: 'chen',
    fallbackPass: saraEnv && saraEnv !== 'chen' ? saraEnv : undefined,
    storagePath: resolve(AUTH_DIR, 'sara.json'),
  });
}
