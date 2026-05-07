/**
 * Allergy display name resolver.
 *
 * 1.5 verification surfaced this rule on Gloria Tran's Sulfonamide allergy:
 * when OpenEMR cannot map the allergen to a coded concept it emits
 * `code.coding[0] = { code: "unknown", display: "Unknown" }` (the FHIR
 * data-absent-reason convention). The actual allergen name is stashed in
 * `text.div` as XHTML.
 *
 * Resolution order:
 *   1. `code.text` if non-empty
 *   2. `code.coding[].display` if present and not literally "Unknown"
 *   3. `text.div` with HTML stripped
 *   4. fallback "Unknown allergen"
 */

import type { FhirAllergyIntolerance } from "./types";

const FALLBACK = "Unknown allergen";

export function allergyDisplay(a: FhirAllergyIntolerance): string {
  const text = a.code?.text?.trim();
  if (text) {
    return text;
  }

  for (const coding of a.code?.coding ?? []) {
    const display = coding.display?.trim();
    if (display && display.toLowerCase() !== "unknown") {
      return display;
    }
  }

  const fromDiv = stripHtml(a.text?.div ?? "").trim();
  if (fromDiv) {
    return fromDiv;
  }

  return FALLBACK;
}

function stripHtml(input: string): string {
  // Remove tags, then collapse whitespace. Decoding the small set of named
  // entities that appear in OpenEMR's narrative payloads is enough — we are
  // not trying to be a general-purpose HTML parser.
  const noTags = input.replace(/<[^>]*>/g, " ");
  const decoded = noTags
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, " ");
  return decoded.replace(/\s+/g, " ");
}
