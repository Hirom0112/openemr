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
import { EditPencilButton } from "./EditPencilButton";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorCard } from "@/components/cards/card-states";
import { FhirClient } from "@/lib/fhir/client";
import { authSessionTokenProvider } from "@/lib/fhir/token";
import type {
  FhirCareTeam,
  FhirCareTeamParticipant,
} from "@/lib/fhir/types";

const CARD_TITLE = "Care Team";

/**
 * Eight column headers from `manage_care_team.html.twig:189–202`. The
 * original card renders this thead unconditionally; the empty state is
 * the headers + an empty body row (see `dashboard-inventory.md` §
 * Care Team → States → Empty). Type / Since / Status / Note / Remove
 * have no FHIR mapping for the loaded state — the column headers still
 * render to match parity, with the cells empty.
 */
const CARE_TEAM_COLUMNS = [
    "Type",
    "Member",
    "Role",
    "Facility",
    "Since",
    "Status",
    "Note",
    "Remove",
] as const;

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

  // Parity correction (2026-05-08): the previous implementation
  // short-circuited the empty state to `<EmptyCard message="None" />`.
  // Per `dashboard-inventory.md` § Care Team → States → Empty, the
  // original card renders the eight-column header row with an empty
  // body. Restoring that here.
  const firstNamedTeam = careTeams.find(
    (t) => (t.name && t.name.trim().length > 0) || t.status,
  );
  const teamName = firstNamedTeam?.name?.trim();
  const teamStatus = firstNamedTeam?.status;

  return (
    <Card size="sm" data-testid="care-team-card">
      <CardHeader className="grid-cols-[1fr_auto] items-center">
        <CardTitle className="text-primary">{CARD_TITLE}</CardTitle>
        <EditPencilButton
          ariaLabel="Edit care team"
          testId="care-team-edit"
          cardName="CareTeamCard"
        />
      </CardHeader>
      <CardContent>
        {teamName || teamStatus ? (
          <div
            className="mb-2 flex items-center gap-2"
            data-testid="care-team-header"
          >
            {teamName ? (
              <h5
                className="text-sm font-semibold"
                data-testid="care-team-name"
              >
                {teamName}
              </h5>
            ) : null}
            {teamStatus ? (
              <span
                className="rounded border px-1.5 py-0.5 text-xs text-muted-foreground"
                data-testid="care-team-status"
              >
                {teamStatus}
              </span>
            ) : null}
          </div>
        ) : null}
        <table
          className="w-full text-sm"
          data-testid="care-team-table"
        >
          <thead className="bg-muted text-xs text-muted-foreground">
            <tr>
              {CARE_TEAM_COLUMNS.map((col) => (
                <th
                  key={col}
                  scope="col"
                  className="px-2 py-1 text-left font-medium"
                >
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody data-testid="care-team-tbody">
            {participants.length === 0 ? (
              // Empty body matches `manage_care_team.html.twig:204` — a
              // single empty row whose bottom border produces the faint
              // divider line in `reference-screenshots/15-card-careteam-empty.png`.
              <tr data-testid="care-team-empty-row">
                {CARE_TEAM_COLUMNS.map((col) => (
                  <td key={col} className="px-2 py-2">&nbsp;</td>
                ))}
              </tr>
            ) : (
              participants.map((participant, index) => {
                const member = memberDisplay(participant);
                const role = roleText(participant);
                const facility = participant.onBehalfOf?.display?.trim() ?? "";
                const key =
                  participant.member?.reference ?? `${member ?? "member"}-${index}`;
                // Type / Since / Status / Note / Remove have no FHIR
                // mapping; cells render empty to preserve column alignment.
                return (
                  <tr key={key} data-testid="care-team-row">
                    <td className="px-2 py-1">&nbsp;</td>
                    <td className="px-2 py-1 font-medium" data-testid="care-team-member">
                      {member ?? "Unknown member"}
                    </td>
                    <td className="px-2 py-1" data-testid="care-team-role">
                      {role ?? ""}
                    </td>
                    <td className="px-2 py-1" data-testid="care-team-facility">
                      {facility}
                    </td>
                    <td className="px-2 py-1">&nbsp;</td>
                    <td className="px-2 py-1">&nbsp;</td>
                    <td className="px-2 py-1">&nbsp;</td>
                    <td className="px-2 py-1">&nbsp;</td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
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
