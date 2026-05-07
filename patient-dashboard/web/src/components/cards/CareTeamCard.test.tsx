// @vitest-environment jsdom
/**
 * CareTeamCard render tests.
 *
 * The "loaded" fixture below is built from the INFERRED FHIR row shape
 * documented in patient-dashboard/dashboard-api-map.md ("Loaded-state
 * shape inferred only" warning) — no synthetic OpenEMR patient has any
 * care team members, so the loaded path has not been validated against a
 * live response. These tests exercise the rendering contract; they do not
 * prove the wire shape.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { CareTeamCardView } from "./CareTeamCard";
import { ErrorCard } from "./card-states";
import type { FhirCareTeam } from "@/lib/fhir/types";

describe("CareTeamCardView (render)", () => {
  it("renders the eight-column header table with an empty body when no teams are returned", () => {
    // Parity correction (2026-05-08): the previous behaviour collapsed
    // this state to `<EmptyCard message="None" />`. The original card
    // (`manage_care_team.html.twig:189–207`,
    // `reference-screenshots/15-card-careteam-empty.png`) renders the
    // eight-column thead + an empty body row.
    render(<CareTeamCardView careTeams={[]} />);

    expect(screen.queryByTestId("empty-card")).toBeNull();
    expect(screen.getByTestId("care-team-card")).toBeTruthy();

    const table = screen.getByTestId("care-team-table");
    const headerCells = table.querySelectorAll("thead th");
    expect(headerCells).toHaveLength(8);
    const headers = Array.from(headerCells).map((c) => c.textContent);
    expect(headers).toEqual([
      "Type",
      "Member",
      "Role",
      "Facility",
      "Since",
      "Status",
      "Note",
      "Remove",
    ]);

    // Empty body row matches the faint-divider treatment in screenshot 15.
    expect(screen.getByTestId("care-team-empty-row")).toBeTruthy();
  });

  it("renders the empty body row when teams exist but have no participants", () => {
    const teams: FhirCareTeam[] = [
      {
        resourceType: "CareTeam",
        id: "team-1",
        status: "active",
        name: "Primary",
        participant: [],
      },
    ];
    render(<CareTeamCardView careTeams={teams} />);

    expect(screen.getByTestId("care-team-empty-row")).toBeTruthy();
    // Team-level header still surfaces name + status above the table.
    expect(screen.getByTestId("care-team-name").textContent).toBe("Primary");
    expect(screen.getByTestId("care-team-status").textContent).toBe("active");
  });

  it("renders an inferred-shape participant row (member.display + role.text)", () => {
    // INFERRED shape — matches the FhirCareTeamService source per the API
    // map. Not validated against a live response (no synthetic care team
    // exists). When 1.5 verification seeds a real care team, revisit.
    const teams: FhirCareTeam[] = [
      {
        resourceType: "CareTeam",
        id: "team-1",
        status: "active",
        name: "Primary",
        participant: [
          {
            member: {
              reference: "Practitioner/abc",
              display: "Dr. Alice Example",
            },
            role: [{ text: "Primary Care Provider" }],
            onBehalfOf: { display: "Springfield Clinic" },
          },
        ],
      },
    ];
    render(<CareTeamCardView careTeams={teams} />);

    const row = screen.getByTestId("care-team-row");
    expect(row).toBeTruthy();
    expect(screen.getByTestId("care-team-member").textContent).toBe(
      "Dr. Alice Example",
    );
    expect(screen.getByTestId("care-team-role").textContent).toBe(
      "Primary Care Provider",
    );
    expect(screen.getByTestId("care-team-facility").textContent).toBe(
      "Springfield Clinic",
    );
    // Edit pencil affordance is present in the loaded chrome.
    expect(screen.getByTestId("care-team-edit")).toBeTruthy();
  });

  it("gracefully omits the facility cell when onBehalfOf.display is absent", () => {
    const teams: FhirCareTeam[] = [
      {
        resourceType: "CareTeam",
        id: "team-1",
        status: "active",
        participant: [
          {
            member: { display: "Dr. Bob Example" },
            role: [{ text: "Cardiologist" }],
          },
        ],
      },
    ];
    render(<CareTeamCardView careTeams={teams} />);

    expect(screen.getByTestId("care-team-member").textContent).toBe(
      "Dr. Bob Example",
    );
    // Facility cell still renders (column alignment requires it) but
    // its text is empty when `onBehalfOf.display` is absent.
    expect(screen.getByTestId("care-team-facility").textContent).toBe("");
  });
});

describe("CareTeamCard error path", () => {
  it("renders ErrorCard with the card title for the error state", () => {
    // The default async export catches FHIR errors and returns
    // <ErrorCard title="Care Team" />. We assert the rendered ErrorCard
    // shape directly (rather than spinning up a fake FhirClient) because
    // the catch-and-render contract is what callers depend on.
    render(<ErrorCard title="Care Team" />);

    const alert = screen.getByTestId("error-card");
    expect(alert.getAttribute("role")).toBe("alert");
    expect(alert.textContent).toContain("Care Team");
  });
});
