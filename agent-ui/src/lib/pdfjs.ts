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
  // The bundle is loaded as a plain <script> tag (not type="module"),
  // so `import.meta` is a syntax error in this context. We resolve the
  // worker URL by finding our own <script> element in the DOM and
  // joining the worker filename against its src. The worker file sits
  // next to copilot.js in the deployed asset directory.
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
    // Fall through to the relative-URL fallback.
  }
  return './pdf.worker.min.mjs';
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
