/**
 * MedicationsCard — "Medications" sub-card on the patient dashboard.
 *
 * Follows the PatientHeader pattern: an async Server Component fetches
 * FHIR `MedicationRequest` server-side using the Auth.js session token,
 * filters via the shared `filterMedications` synthesis helper (status
 * in active|on-hold|completed), and renders.
 *
 * Spec sources (treat as authoritative — fields are not invented here):
 *   - patient-dashboard/dashboard-inventory.md  -> "Medications"
 *   - patient-dashboard/dashboard-api-map.md    -> "Medications"
 *   - patient-dashboard/reference-screenshots/13-card-medications-loaded.png
 *
 * Synthesis: do NOT re-implement filtering — call `filterMedications`
 * from `@/lib/fhir/synthesis`. That helper carries the inventory's
 * status-only rule verbatim and is the single source of truth.
 */
import * as React from "react";
import { Pencil } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyCard, ErrorCard } from "@/components/cards/card-states";
import { FhirClient } from "@/lib/fhir/client";
import { authSessionTokenProvider } from "@/lib/fhir/token";
import { filterMedications } from "@/lib/fhir/synthesis";
import type { FhirMedicationRequest } from "@/lib/fhir/types";

const CARD_TITLE = "Medications";

interface MedicationsCardProps {
  patientUuid: string;
}

/**
 * Format a single MedicationRequest into the row's display string.
 * Per the inventory + 1.5 verification for Gloria, the synthetic dataset
 * collapses drug + dosage into `medicationCodeableConcept.text`
 * ("Furosemide 80mg IV"). When `dosageInstruction[0].text` is also
 * present we append it; when neither is populated we fall back through
 * `coding[0].display` and finally a literal "Unknown medication" so a
 * malformed row never crashes the card.
 */
export function formatMedicationDisplay(req: FhirMedicationRequest): string {
  const concept = req.medicationCodeableConcept;
  const conceptText = concept?.text?.trim();
  const codingDisplay = concept?.coding?.[0]?.display?.trim();
  const name =
    (conceptText && conceptText.length > 0 && conceptText) ||
    (codingDisplay && codingDisplay.length > 0 && codingDisplay) ||
    "Unknown medication";

  const dosage = req.dosageInstruction?.[0]?.text?.trim();
  if (dosage && dosage.length > 0 && dosage !== name) {
    return `${name} — ${dosage}`;
  }
  return name;
}

/**
 * Skeleton companion. Mirrors the EmptyCard chrome (titled CardHeader)
 * with a stack of row-shaped skeletons in the body — the loaded card
 * is itself a list of medication rows, so the skeleton mimics that.
 */
export function MedicationsCardSkeleton(): React.ReactElement {
  return (
    <Card size="sm" data-testid="medications-card-skeleton" aria-busy="true">
      <CardHeader>
        <CardTitle>{CARD_TITLE}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-4 w-1/2" />
      </CardContent>
    </Card>
  );
}

interface MedicationsCardViewProps {
  medications: readonly FhirMedicationRequest[];
}

/**
 * Pure-presentation view component, exported so the test suite can
 * render it directly without faking an async Server Component.
 *
 * Note: callers must pass an already-filtered list (use
 * `filterMedications` from `@/lib/fhir/synthesis`). Filtering is not
 * repeated here — the entry-point Server Component below applies it.
 */
export function MedicationsCardView({
  medications,
}: MedicationsCardViewProps): React.ReactElement {
  if (medications.length === 0) {
    return <EmptyCard title={CARD_TITLE} message="None" />;
  }

  return (
    <Card size="sm" data-testid="medications-card">
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle>{CARD_TITLE}</CardTitle>
        {/* Edit pencil — STUB. Per inventory: pencil icon -> load_location
            to stats_full.php?active=all&category=medication. Wiring to a
            real edit route is out of scope for the read-only card. */}
        <button
          type="button"
          aria-label="Edit medications"
          data-testid="medications-edit"
          className="inline-flex h-6 w-6 items-center justify-center rounded text-primary hover:bg-muted"
          onClick={() => {
            // TODO(phase-5): wire to medications edit route.
            console.log("[MedicationsCard] edit clicked (TODO: wire route)");
          }}
        >
          <Pencil className="h-4 w-4" />
        </button>
      </CardHeader>
      <CardContent className="flex flex-col divide-y">
        {medications.map((med) => {
          const display = formatMedicationDisplay(med);
          // Status is suppressed for the common "active" case; when
          // a non-active status survives the filter (on-hold / completed)
          // we surface it parenthetically so the user knows the row is
          // not a currently-being-taken med.
          const showStatus = med.status !== "active";
          return (
            <div
              key={med.id}
              data-testid="medication-row"
              data-status={med.status}
              className="py-1 text-sm"
            >
              <span data-testid="medication-name">{display}</span>
              {showStatus ? (
                <span
                  className="ml-2 text-muted-foreground"
                  data-testid="medication-status"
                >
                  ({med.status})
                </span>
              ) : null}
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}

/**
 * Server Component entry point. Builds a FhirClient from the ambient
 * Auth.js session, fetches MedicationRequest for the patient, applies
 * the shared `filterMedications` synthesis helper, and renders.
 *
 * Errors are caught and surfaced via ErrorCard so a transient FHIR
 * failure does not blank the page.
 */
export default async function MedicationsCard({
  patientUuid,
}: MedicationsCardProps): Promise<React.ReactElement> {
  const baseUrl = process.env.OPENEMR_BASE_URL;
  if (!baseUrl) {
    return (
      <ErrorCard
        title={CARD_TITLE}
        message="Server is not configured to reach OpenEMR."
      />
    );
  }

  try {
    const client = new FhirClient({
      baseUrl: `${baseUrl.replace(/\/+$/, "")}/apis/default/fhir`,
      tokenProvider: authSessionTokenProvider,
    });
    const requests = await client.getMedicationRequests(patientUuid);
    const medications = filterMedications(requests);
    return <MedicationsCardView medications={medications} />;
  } catch (error) {
    console.error("[MedicationsCard] FHIR fetch failed", error);
    return <ErrorCard title={CARD_TITLE} />;
  }
}
