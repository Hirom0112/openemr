import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import type { CopilotConfig } from './types';
import { initAuthToken } from './auth/jwt';

function readConfig(): CopilotConfig {
  // index.php injects config as a JSON script tag (CSP-safe, no eval).
  const el = document.getElementById('copilot-config');
  if (el?.textContent) {
    try {
      const config = JSON.parse(el.textContent) as CopilotConfig;
      // api.ts reads window.__COPILOT_CONFIG__ — keep it in sync.
      window.__COPILOT_CONFIG__ = config;
      return config;
    } catch {
      // fall through
    }
  }
  // Fallback: legacy window global (standalone dev).
  return window.__COPILOT_CONFIG__ ?? ({} as CopilotConfig);
}

const root = document.getElementById('copilot-root');
if (root) {
  const config = readConfig();
  // Seed the in-memory token holder before any API call fires. Absent jwt
  // (server-side COPILOT_JWT_SECRET unset) leaves the holder null and api.ts
  // skips the Authorization header — preserves dev-mode parity.
  initAuthToken(config.jwt);
  createRoot(root).render(
    <StrictMode>
      <App config={config} />
    </StrictMode>
  );
}
