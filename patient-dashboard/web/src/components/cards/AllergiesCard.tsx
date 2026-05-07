/**
 * AllergiesCard — server-rendered Allergies card for the patient dashboard.
 *
 * Follows the PatientHeader Server-Component pattern: builds a FhirClient
 * with the Auth.js session token provider, fetches AllergyIntolerance
 * resources for the patient uuid, and renders a small list. Errors are
 * surfaced through ErrorCard so a transient FHIR failure does not blank
 * the whole page; an empty bundle is surfaced through EmptyCard.
 *
 * Spec sources (treat as authoritative):
 *   - patient-dashboard/dashboard-inventory.md  -> "Allergies"
 *   - patient-dashboard/dashboard-api-map.md    -> "Allergies"
 *   - patient-dashboard/reference-screenshots/11-card-allergies-loaded.png
 *   - patient-dashboard/reference-screenshots/21-card-allergies-empty.png
 *
 * The orchestrator wires this card into the page after all six cards land —
 * this file deliberately does not touch page.tsx.
 */
import * as React from "react";
import { Pencil } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyCard, ErrorCard } from "@/components/cards/card-states";
import { FhirClient } from "@/lib/fhir/client";
import { authSessionTokenProvider } from "@/lib/fhir/token";
import { allergyDisplay } from "@/lib/fhir/allergy-display";
import type { FhirAllergyIntolerance } from "@/lib/fhir/types";

const CARD_TITLE = "Allergies";
const MAX_VISIBLE = 5;

interface AllergiesCardProps {
  patientUuid: string;
}

interface AllergiesCardViewProps {
  allergies: FhirAllergyIntolerance[];
}

/**
 * Resolve the human-readable reaction for a row.
 *
 * Per dashboard-api-map.md: `reaction[0].manifestation[0].text` falling back
 * to `.coding[0].display`. Inventory renders this as a tooltip; we surface
 * it inline (small, muted) per the task brief while keeping the original
 * tooltip behavior via `title=` for parity.
 */
export function reactionText(allergy: FhirAllergyIntolerance): string | null {
  const manifestation = allergy.reaction?.[0]?.manifestation?.[0];
  if (!manifestation) {
    return null;
  }
  const text = manifestation.text?.trim();
  if (text) {
    return text;
  }
  for (const coding of manifestation.coding ?? []) {
    const display = coding.display?.trim();
    if (display) {
      return display;
    }
  }
  return null;
}

/**
 * Resolve a display label for severity. Per inventory the severity surfaced
 * to the user is the resolved `severity_al` enum label; in the FHIR port
 * that value lands on `reaction[0].severity` (`mild`/`moderate`/`severe`).
 * Returns null when not recorded — matches the screenshot where "Sulfonamide
 * ()" renders empty parens because Gloria's allergy has no severity.
 */
export function severityLabel(allergy: FhirAllergyIntolerance): string | null {
  const sev = allergy.reaction?.[0]?.severity;
  if (!sev) {
    return null;
  }
  return sev;
}

/**
 * Loading skeleton companion. Mirrors the loaded card chrome (title +
 * pencil action) so the affordance is stable across states.
 */
export function AllergiesCardSkeleton(): React.ReactElement {
  return (
    <Card size="sm" data-testid="allergies-card-skeleton" aria-busy="true">
      <CardHeader>
        <CardTitle>{CARD_TITLE}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <Skeleton className="h-3 w-3/4" />
        <Skeleton className="h-3 w-1/2" />
        <Skeleton className="h-3 w-2/3" />
      </CardContent>
    </Card>
  );
}

/**
 * Pure-presentation view component, exported so tests can render it
 * directly without faking an async Server Component.
 */
export function AllergiesCardView({
  allergies,
}: AllergiesCardViewProps): React.ReactElement {
  if (allergies.length === 0) {
    // Brief: "if total=0 (or array is empty), render
    // <EmptyCard title='Allergies' message='None' />".
    // Note: original screenshot 21 shows "No Known Allergies"; the brief
    // explicitly overrides to "None". Deviation logged in the report-back.
    return <EmptyCard title={CARD_TITLE} message="None" />;
  }

  const visible = allergies.slice(0, MAX_VISIBLE);

  return (
    <Card size="sm" data-testid="allergies-card">
      <CardHeader className="grid-cols-[1fr_auto] items-center">
        <CardTitle className="text-primary">{CARD_TITLE}</CardTitle>
        <button
          type="button"
          aria-label="Edit allergies"
          data-testid="allergies-edit"
          // Read-only stub per brief — full wiring lives in a later phase.
          onClick={() => {
            console.log("[AllergiesCard] TODO: wire edit affordance");
          }}
          className="inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-muted"
        >
          <Pencil className="h-4 w-4" aria-hidden="true" />
        </button>
      </CardHeader>
      <CardContent>
        <ul className="flex flex-col gap-1" data-testid="allergies-list">
          {visible.map((allergy) => {
            const allergen = allergyDisplay(allergy);
            const reaction = reactionText(allergy);
            const severity = severityLabel(allergy);
            return (
              <li
                key={allergy.id}
                className="flex flex-wrap items-baseline gap-2"
                data-testid="allergy-row"
                title={reaction ?? undefined}
              >
                <span className="font-medium" data-testid="allergy-name">
                  {allergen} ({severity ?? ""})
                </span>
                {reaction ? (
                  <span
                    className="text-xs text-muted-foreground"
                    data-testid="allergy-reaction"
                  >
                    {reaction}
                  </span>
                ) : null}
                {severity ? (
                  <span
                    className="rounded border px-1.5 py-0.5 text-xs text-muted-foreground"
                    data-testid="allergy-severity"
                  >
                    {severity}
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
 * Server Component entry point. Builds a FhirClient from the ambient
 * Auth.js session, fetches AllergyIntolerance for the patient uuid, and
 * renders the view. Auth gating happens at the page level — this card
 * trusts that the FHIR token provider has a session to consume.
 */
export default async function AllergiesCard({
  patientUuid,
}: AllergiesCardProps): Promise<React.ReactElement> {
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
    const allergies = await client.getAllergyIntolerances(patientUuid);
    return <AllergiesCardView allergies={allergies} />;
  } catch (error) {
    console.error("[AllergiesCard] FHIR fetch failed", error);
    return <ErrorCard title={CARD_TITLE} />;
  }
}
