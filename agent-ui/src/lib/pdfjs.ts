/**
 * pdf.js worker setup + a thin loadPdf helper.
 *
 * The Vite `?url` suffix asks the bundler for a hashed asset URL pointing at
 * the worker bundle, so the worker is served from our own origin (no CDN
 * dependency, CSP-safe). pdfjs-dist v4 ships an ESM worker — we import the
 * `.mjs` build to match.
 */

import * as pdfjs from 'pdfjs-dist';
import type { PDFDocumentProxy } from 'pdfjs-dist';
// @ts-expect-error - Vite's ?url suffix produces a string export at build time
// without ambient typings; the import path resolves at bundle time.
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl as string;

/**
 * Load a PDF from either a URL or raw bytes. Callers should accept the
 * returned proxy and render pages via `getPage(n).render(...)`.
 */
export async function loadPdf(source: string | ArrayBuffer): Promise<PDFDocumentProxy> {
  const params = typeof source === 'string' ? { url: source } : { data: source };
  return pdfjs.getDocument(params).promise;
}

export type { PDFDocumentProxy };
