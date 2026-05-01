import type { Citation } from '../types';

/**
 * Resolves a synthetic patient identifier (e.g. "pt-001", "pt-010") to the
 * numeric OpenEMR PID ("1", "10"). Plain numeric strings or UUIDs are passed
 * through unchanged so real OpenEMR IDs continue to work.
 */
export function resolvePatientPid(patientId: string): string {
  const match = /^pt-0*(\d+)$/.exec(patientId);
  if (match) {
    return match[1];
  }
  return patientId;
}

/**
 * OpenEMR deep-link URL patterns by FHIR resource type.
 * V1: paths are relative — the PHP module runs inside OpenEMR so no baseUrl needed.
 * V2: if the agent panel is ever served cross-origin, prefix with the OpenEMR origin.
 */
const RESOURCE_URL_PATTERNS: Record<string, (c: Citation) => string> = {
  Observation:        (c) => `/interface/orders/results_report.php?pid=${resolvePatientPid(c.patient_id)}`,
  DiagnosticReport:  (c) => `/interface/orders/results_report.php?pid=${resolvePatientPid(c.patient_id)}`,
  MedicationRequest: (c) => `/interface/patient_file/summary/demographics_full.php?set_pid=${resolvePatientPid(c.patient_id)}#medications`,
  MedicationStatement:(c) => `/interface/patient_file/summary/demographics_full.php?set_pid=${resolvePatientPid(c.patient_id)}#medications`,
  Condition:         (c) => `/interface/patient_file/summary/demographics_full.php?set_pid=${resolvePatientPid(c.patient_id)}#problems`,
  AllergyIntolerance:(c) => `/interface/patient_file/summary/demographics_full.php?set_pid=${resolvePatientPid(c.patient_id)}#allergies`,
  Encounter:         (c) => `/interface/patient_file/encounter/encounter_top.php?set_pid=${resolvePatientPid(c.patient_id)}`,
  Flag:              (c) => `/interface/patient_file/summary/demographics_full.php?set_pid=${resolvePatientPid(c.patient_id)}`,
  Patient:           (c) => `/interface/patient_file/summary/demographics_full.php?set_pid=${resolvePatientPid(c.patient_id)}`,
};

/**
 * Returns a relative OpenEMR deep-link URL for the given citation, or null if
 * the resource type has no known URL pattern (falls back to V1 textual badge).
 */
export function buildCitationUrl(citation: Citation): string | null {
  const pattern = RESOURCE_URL_PATTERNS[citation.resource_type];
  if (!pattern) return null;
  return pattern(citation);
}
