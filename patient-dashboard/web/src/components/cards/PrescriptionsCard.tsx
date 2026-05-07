/**
 * PrescriptionsCard — clinician-written prescriptions for a patient.
 *
 * Spec sources (treat as authoritative):
 *   - patient-dashboard/dashboard-inventory.md  -> "Prescriptions"
 *   - patient-dashboard/dashboard-api-map.md    -> "Prescriptions"
 *   - patient-dashboard/reference-screenshots/14-card-prescriptions-empty.png
 *
 * Per the 1.5 verification recorded in the inventory: every patient in the
 * synthetic dataset (pids 4, 5, 13, 24, 26, 27) returns zero
 * `MedicationRequest` resources with `intent=order`. The "None" empty state
 * is therefore the *expected default* for synthetic data, not an error or a
 * fetch failure. We treat the empty bundle as a successful load.
 *
 * Filtering is delegated to `filterPrescriptions` (synthesis.ts), which
 * applies the inventory's documented rule: `intent === "order"` AND
 * `status === "active"` AND `requester != null`.
 */
import * as React from "react";
import { EditPencilButton } from "./EditPencilButton";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyCard, ErrorCard } from "@/components/cards/card-states";
import { FhirClient } from "@/lib/fhir/client";
import { filterPrescriptions } from "@/lib/fhir/synthesis";
import { authSessionTokenProvider } from "@/lib/fhir/token";
import type { FhirMedicationRequest } from "@/lib/fhir/types";

const CARD_TITLE = "Prescriptions";

interface PrescriptionsCardProps {
  patientUuid: string;
}

interface PrescriptionsCardViewProps {
  prescriptions: readonly FhirMedicationRequest[];
}

/**
 * Pull the most useful display string for a prescription's medication name.
 * Inventory maps `prescriptions.drug` -> `medicationCodeableConcept.text`,
 * with the coded display as a secondary fallback.
 */
export function formatMedicationName(req: FhirMedicationRequest): string {
  const cc = req.medicationCodeableConcept;
  const text = cc?.text?.trim();
  if (text) {
    return text;
  }
  const codingDisplay = cc?.coding?.find((c) => c.display?.trim())?.display?.trim();
  if (codingDisplay) {
    return codingDisplay;
  }
  const refDisplay = req.medicationReference?.display?.trim();
  if (refDisplay) {
    return refDisplay;
  }
  return "Unknown medication";
}

/**
 * Per the API map, dosage/form/unit/route/interval all collapse into FHIR's
 * `dosageInstruction` shape. We surface `dosageInstruction[0].text` as the
 * primary one-liner — that's what OpenEMR's FHIR service emits.
 */
export function formatDosage(req: FhirMedicationRequest): string | null {
  const text = req.dosageInstruction?.[0]?.text?.trim();
  return text && text.length > 0 ? text : null;
}

export function formatPrescriber(req: FhirMedicationRequest): string | null {
  const display = req.requester?.display?.trim();
  return display && display.length > 0 ? display : null;
}

/**
 * Skeleton companion. Matches the EmptyCard chrome (header + single body
 * line) so the layout doesn't jump when the fetch resolves.
 */
export function PrescriptionsCardSkeleton(): React.ReactElement {
  return (
    <Card size="sm" data-testid="prescriptions-card-skeleton" aria-busy="true">
      <CardHeader>
        <CardTitle>{CARD_TITLE}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <Skeleton className="h-3 w-2/3" />
        <Skeleton className="h-3 w-1/2" />
      </CardContent>
    </Card>
  );
}

/**
 * Pure-presentation view. Exported so tests render it directly without
 * faking a Server Component. Empty bundles are rendered as `EmptyCard`
 * with "None" — that is the documented loaded-but-empty state, not an
 * error.
 */
export function PrescriptionsCardView({
  prescriptions,
}: PrescriptionsCardViewProps): React.ReactElement {
  if (prescriptions.length === 0) {
    return <EmptyCard title={CARD_TITLE} message="None" />;
  }

  return (
    <Card size="sm" data-testid="prescriptions-card">
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle>{CARD_TITLE}</CardTitle>
        <EditPencilButton
          ariaLabel="Edit prescriptions (not yet wired)"
          testId="prescriptions-edit"
          cardName="PrescriptionsCard"
        />
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <ul className="flex flex-col gap-2" data-testid="prescriptions-list">
          {prescriptions.map((rx) => {
            const name = formatMedicationName(rx);
            const dosage = formatDosage(rx);
            const prescriber = formatPrescriber(rx);
            return (
              <li
                key={rx.id}
                className="flex flex-col"
                data-testid="prescription-row"
              >
                <span className="font-medium" data-testid="prescription-name">
                  {name}
                </span>
                {dosage ? (
                  <span
                    className="text-sm text-muted-foreground"
                    data-testid="prescription-dosage"
                  >
                    {dosage}
                  </span>
                ) : null}
                {prescriber ? (
                  <span
                    className="text-xs text-muted-foreground"
                    data-testid="prescription-prescriber"
                  >
                    Prescribed by {prescriber}
                  </span>
                ) : null}
              </li>
            );
          })}
        </ul>
      </CardContent>
    </Card>
  );
}

/**
 * Server Component entry point. Mirrors `PatientHeader`: build a FhirClient
 * from the ambient session, fetch MedicationRequests for the patient, run
 * them through the documented `filterPrescriptions` synthesis rule, and
 * render. A truly empty bundle is the success path, not the error path.
 */
export default async function PrescriptionsCard({
  patientUuid,
}: PrescriptionsCardProps): Promise<React.ReactElement> {
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
    const prescriptions = filterPrescriptions(requests);
    return <PrescriptionsCardView prescriptions={prescriptions} />;
  } catch (error) {
    console.error("[PrescriptionsCard] FHIR fetch failed", error);
    return <ErrorCard title={CARD_TITLE} />;
  }
}
