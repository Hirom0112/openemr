/**
 * CareTeamCard — server-rendered Care Team card for the patient dashboard.
 *
 * Follows the PatientHeader / AllergiesCard Server-Component pattern: build
 * a FhirClient from the Auth.js session token provider, fetch CareTeam
 * resources for the patient uuid, render a small table.
 *
 * Spec sources (treat as authoritative):
 *   - patient-dashboard/dashboard-inventory.md  -> "Care Team"
 *   - patient-dashboard/dashboard-api-map.md    -> "Care Team"
 *   - patient-dashboard/reference-screenshots/15-card-careteam-empty.png
 *
 * Empty is THE expected state for every synthetic patient (1.5 verified:
 * `care_teams` and `care_team_member` are empty for every fixture). The
 * loaded-state row shape below is INFERRED from the FhirCareTeamService
 * source — not from a live response. See dashboard-api-map.md "Loaded-state
 * shape inferred only" warning.
 */
import * as React from "react";
import { Pencil } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyCard, ErrorCard } from "@/components/cards/card-states";
import { FhirClient } from "@/lib/fhir/client";
import { authSessionTokenProvider } from "@/lib/fhir/token";
import type {
  FhirCareTeam,
  FhirCareTeamParticipant,
} from "@/lib/fhir/types";

const CARD_TITLE = "Care Team";

interface CareTeamCardProps {
  patientUuid: string;
}

interface CareTeamCardViewProps {
  careTeams: FhirCareTeam[];
}

/**
 * Flatten a list of CareTeam resources into a flat list of participant
 * rows. Each FHIR CareTeam may contain multiple participants; the original
 * dashboard renders one row per member regardless of which team they belong
 * to.
 */
export function flattenParticipants(
  careTeams: FhirCareTeam[],
): FhirCareTeamParticipant[] {
  return careTeams.flatMap((team) => team.participant ?? []);
}

/**
 * Resolve a member display name from `participant.member.display`
 * (US Core 3.1.1 — see dashboard-api-map.md "Care Team" row).
 */
export function memberDisplay(
  participant: FhirCareTeamParticipant,
): string | null {
  const display = participant.member?.display?.trim();
  return display && display.length > 0 ? display : null;
}

/**
 * Resolve role text. Prefer `role[0].text`, fall back to
 * `role[0].coding[0].display` per the API map.
 */
export function roleText(
  participant: FhirCareTeamParticipant,
): string | null {
  const role = participant.role?.[0];
  if (!role) {
    return null;
  }
  const text = role.text?.trim();
  if (text) {
    return text;
  }
  for (const coding of role.coding ?? []) {
    const display = coding.display?.trim();
    if (display) {
      return display;
    }
  }
  return null;
}

/**
 * Loading skeleton companion. Mirrors the loaded card chrome (title +
 * pencil action) so the affordance is stable across states.
 */
export function CareTeamCardSkeleton(): React.ReactElement {
  return (
    <Card size="sm" data-testid="care-team-card-skeleton" aria-busy="true">
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
export function CareTeamCardView({
  careTeams,
}: CareTeamCardViewProps): React.ReactElement {
  const participants = flattenParticipants(careTeams);

  if (participants.length === 0) {
    // Parity simplification: the original OpenEMR card renders an empty
    // table header with no rows for a patient who has no care team
    // (reference-screenshots/15-card-careteam-empty.png). The brief
    // collapses that to the standard EmptyCard "None" treatment so all
    // empty cards on the dashboard speak the same visual language.
    return <EmptyCard title={CARD_TITLE} message="None" />;
  }

  return (
    <Card size="sm" data-testid="care-team-card">
      <CardHeader className="grid-cols-[1fr_auto] items-center">
        <CardTitle className="text-primary">{CARD_TITLE}</CardTitle>
        <button
          type="button"
          aria-label="Edit care team"
          data-testid="care-team-edit"
          // Read-only stub per brief — full wiring lives in a later phase.
          onClick={() => {
            console.log("[CareTeamCard] TODO: wire edit affordance");
          }}
          className="inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-muted"
        >
          <Pencil className="h-4 w-4" aria-hidden="true" />
        </button>
      </CardHeader>
      <CardContent>
        {/* Inventory note: per-row "Status" and "Note" columns have no
            FHIR equivalent (api-map: "FHIR has no per-participant status"
            and "Note ... NOT FOUND on participant in FHIR R4"). We render
            Member, Role, Facility only. Facility is omitted from the row
            when `onBehalfOf.display` is absent rather than rendering an
            empty cell. */}
        <ul className="flex flex-col gap-1" data-testid="care-team-list">
          {participants.map((participant, index) => {
            const member = memberDisplay(participant);
            const role = roleText(participant);
            const facility = participant.onBehalfOf?.display?.trim() || null;
            const key =
              participant.member?.reference ?? `${member ?? "member"}-${index}`;
            return (
              <li
                key={key}
                className="flex flex-wrap items-baseline gap-2"
                data-testid="care-team-row"
              >
                <span className="font-medium" data-testid="care-team-member">
                  {member ?? "Unknown member"}
                </span>
                {role ? (
                  <span
                    className="text-sm text-muted-foreground"
                    data-testid="care-team-role"
                  >
                    {role}
                  </span>
                ) : null}
                {facility ? (
                  <span
                    className="text-xs text-muted-foreground"
                    data-testid="care-team-facility"
                  >
                    {facility}
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
 * Auth.js session, fetches CareTeam for the patient uuid, and renders
 * the view. Auth gating happens at the page level — this card trusts that
 * the FHIR token provider has a session to consume.
 */
export default async function CareTeamCard({
  patientUuid,
}: CareTeamCardProps): Promise<React.ReactElement> {
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
    const careTeams = await client.getCareTeams(patientUuid);
    return <CareTeamCardView careTeams={careTeams} />;
  } catch (error) {
    console.error("[CareTeamCard] FHIR fetch failed", error);
    return <ErrorCard title={CARD_TITLE} />;
  }
}
