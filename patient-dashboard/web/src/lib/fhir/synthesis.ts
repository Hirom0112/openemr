/**
 * Synthesis helpers for the Medications and Prescriptions cards.
 *
 * OpenEMR does not implement `MedicationStatement` (verified during 1.4
 * architecture audit). Both cards therefore project from `MedicationRequest`
 * with different filters. Rules carried forward verbatim from
 * `dashboard-inventory.md`.
 */

import type { FhirMedicationRequest } from "./types";

const MEDICATION_STATUSES: ReadonlySet<FhirMedicationRequest["status"]> =
  new Set(["active", "on-hold", "completed"]);

/**
 * Medications card: status is currently-being-taken, intent is permissive.
 *
 * Per the inventory the intent is intentionally broader than Prescriptions;
 * the inventory's documented rule is that any active `MedicationRequest`
 * shows up here regardless of who wrote it. We therefore filter on status
 * only, matching the inventory's "compromise" paragraph.
 */
export function filterMedications(
  reqs: readonly FhirMedicationRequest[],
): FhirMedicationRequest[] {
  return reqs.filter((r) => MEDICATION_STATUSES.has(r.status));
}

/**
 * Prescriptions card: clinician-written prescriptions only.
 *
 * 1.5 verification confirmed this returns empty for every patient in the
 * synthetic dataset — every entry emits `intent=plan` with `requester`
 * absent. That matches the original dashboard's "None" state for Gloria.
 */
export function filterPrescriptions(
  reqs: readonly FhirMedicationRequest[],
): FhirMedicationRequest[] {
  return reqs.filter(
    (r) =>
      r.intent === "order" &&
      r.status === "active" &&
      r.requester != null,
  );
}
