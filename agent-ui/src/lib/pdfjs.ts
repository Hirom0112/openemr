/**
 * pdf.js worker setup + a thin loadPdf helper.
 *
 * pdfjs-dist v4 ships an ESM worker — we serve `pdf.worker.min.mjs` from
 * the same directory as `copilot.js` (Vite copies it into the bundle's
 * output dir). The bundle is loaded from a deep OpenEMR sub-path
 * (`/interface/modules/custom_modules/oe-module-clinical-copilot/public/`)
 * so a Vite `?url` import resolves to an absolute root-relative URL that
 * 404s. Resolve the worker URL at runtime from the loaded script's own
 * location instead — sidesteps the bundler vs deep-mount mismatch and
 * is CSP-safe (same origin as the iframe).
 */

import * as pdfjs from 'pdfjs-dist';
import type { PDFDocumentProxy } from 'pdfjs-dist';

function resolveWorkerUrl(): string {
  // import.meta.url points at the bundle's own URL when loaded as a module.
  // The worker file sits next to it. URL resolution handles deep sub-paths.
  try {
    if (typeof document !== 'undefined') {
      const scripts = document.getElementsByTagName('script');
      for (let i = scripts.length - 1; i >= 0; i--) {
        const src = scripts[i]?.src || '';
        if (src.endsWith('/copilot.js') || src.includes('copilot.js?')) {
          return new URL('./pdf.worker.min.mjs', src).toString();
        }
      }
    }
  } catch {
    // Fall through to the import.meta.url path below.
  }
  try {
    return new URL('./pdf.worker.min.mjs', import.meta.url).toString();
  } catch {
    return './pdf.worker.min.mjs';
  }
}

pdfjs.GlobalWorkerOptions.workerSrc = resolveWorkerUrl();

/**
 * Load a PDF from either a URL or raw bytes. Callers should accept the
 * returned proxy and render pages via `getPage(n).render(...)`.
 */
export async function loadPdf(source: string | ArrayBuffer): Promise<PDFDocumentProxy> {
  const params = typeof source === 'string' ? { url: source } : { data: source };
  return pdfjs.getDocument(params).promise;
}

export type { PDFDocumentProxy };
