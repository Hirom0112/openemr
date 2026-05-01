import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import type { CopilotConfig } from './types';

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
  createRoot(root).render(
    <StrictMode>
      <App config={readConfig()} />
    </StrictMode>
  );
}
