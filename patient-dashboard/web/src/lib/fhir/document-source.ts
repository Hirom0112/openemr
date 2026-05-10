/**
 * Helpers for extracting the Co-Pilot source-document linkage off a FHIR
 * Condition or Observation row. The Co-Pilot module surfaces provenance two
 * different ways depending on the resource shape:
 *
 *   - `Observation`: `derivedFrom[0].reference = "DocumentReference/<uuid>"`
 *     and `note[0].text = "Source: <locator>"`.
 *   - `Condition`: no top-level `derivedFrom` slot in FHIR R4, so the same
 *     payload is folded into a single `note[0].text` of
 *     `"DocumentReference/<uuid> · Source: <locator>"`.
 *
 * (See `interface/modules/custom_modules/oe-module-clinical-copilot/src/FHIR/
 * FhirObservationCopilotService.php` and `FhirConditionCopilotService.php`.)
 *
 * Parsing is purely string-level. We accept both 32-char hex and dashed UUID
 * forms — UuidRegistry on the OpenEMR side normalises both downstream.
 */
import type {
  FhirAnnotation,
  FhirCondition,
  FhirObservation,
  FhirReference,
} from "./types";

/** Parsed shape of a Co-Pilot source-document linkage. */
export interface DocumentSource {
  /** DocumentReference uuid (32-char hex or dashed). */
  uuid: string;
  /** Citation locator portion ("Source: p1-b042"), if present. */
  locator: string | null;
  /** The full original note text — used as caption / fallback display. */
  rawNote: string;
}

const UUID_RE = /DocumentReference\/([0-9a-fA-F-]{32,36})/;

function firstNoteText(notes: FhirAnnotation[] | undefined): string | null {
  const text = notes?.[0]?.text?.trim();
  return text && text.length > 0 ? text : null;
}

function uuidFromReference(ref: FhirReference | undefined): string | null {
  const r = ref?.reference?.trim();
  if (!r) return null;
  const m = UUID_RE.exec(r);
  return m ? m[1] : null;
}

function locatorFromNote(note: string): string | null {
  // Strip an optional leading "DocumentReference/<uuid> · " prefix; what
  // remains is the human-readable locator (e.g. "Source: p1-b042").
  const stripped = note
    .replace(/^DocumentReference\/[0-9a-fA-F-]+\s*·\s*/, "")
    .trim();
  if (stripped.length === 0) return null;
  // If nothing changed and the whole note was a DocumentReference, no locator.
  if (stripped === note && /^DocumentReference\//.test(note)) return null;
  return stripped;
}

export function parseObservationSource(
  obs: FhirObservation,
): DocumentSource | null {
  const note = firstNoteText(obs.note);
  const uuid =
    uuidFromReference(obs.derivedFrom?.[0]) ??
    (note ? UUID_RE.exec(note)?.[1] ?? null : null);
  if (!uuid && !note) return null;
  // For Observation the locator is the whole note (it's just "Source: …").
  const locator = note ?? null;
  return {
    uuid: uuid ?? "",
    locator,
    rawNote: note ?? (uuid ? `DocumentReference/${uuid}` : ""),
  };
}

export function parseConditionSource(
  c: FhirCondition,
): DocumentSource | null {
  const note = firstNoteText(c.note);
  if (!note) return null;
  const m = UUID_RE.exec(note);
  const uuid = m ? m[1] : null;
  return {
    uuid: uuid ?? "",
    locator: locatorFromNote(note),
    rawNote: note,
  };
}

/** True when the parsed source has a usable DocumentReference uuid. */
export function hasDocumentRef(src: DocumentSource | null): src is DocumentSource {
  return src !== null && src.uuid.length > 0;
}
