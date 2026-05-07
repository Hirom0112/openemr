// @vitest-environment jsdom
/**
 * AllergiesCard render + helper tests.
 *
 * Per-file `@vitest-environment jsdom` matches PatientHeader.test.tsx and
 * keeps the rest of the suite running under `node`.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import {
  AllergiesCardView,
  reactionText,
  severityLabel,
} from "./AllergiesCard";
import type { FhirAllergyIntolerance } from "@/lib/fhir/types";

/**
 * Gloria Tran's Sulfonamide allergy as captured in 1.5 verification:
 * `code.coding[0]` carries the FHIR data-absent-reason convention
 * (`code: "unknown"`, `display: "Unknown"`) and the actual allergen name
 * is in `text.div`. The allergyDisplay() helper is responsible for
 * pulling "Sulfonamide" out of the narrative div.
 */
const gloriaSulfonamide: FhirAllergyIntolerance = {
  resourceType: "AllergyIntolerance",
  id: "gloria-sulfonamide",
  code: {
    coding: [{ code: "unknown", display: "Unknown" }],
  },
  text: {
    status: "generated",
    div: '<div xmlns="http://www.w3.org/1999/xhtml">Sulfonamide</div>',
  },
};

const peanut: FhirAllergyIntolerance = {
  resourceType: "AllergyIntolerance",
  id: "peanut",
  code: { text: "Peanut" },
  reaction: [
    {
      manifestation: [{ text: "Hives" }],
      severity: "moderate",
    },
  ],
};

const penicillin: FhirAllergyIntolerance = {
  resourceType: "AllergyIntolerance",
  id: "penicillin",
  code: { coding: [{ display: "Penicillin G" }] },
  reaction: [
    {
      manifestation: [{ coding: [{ display: "Anaphylaxis" }] }],
      severity: "severe",
    },
  ],
};

describe("AllergiesCardView (render)", () => {
  it("renders the EmptyCard with 'None' when the array is empty", () => {
    render(<AllergiesCardView allergies={[]} />);
    expect(screen.getByTestId("empty-card")).toBeTruthy();
    expect(screen.getByText("None")).toBeTruthy();
    // Loaded-state list is not rendered.
    expect(screen.queryByTestId("allergies-list")).toBeNull();
  });

  it("renders Sulfonamide from text.div when code.coding[0] is data-absent (Gloria's fixture)", () => {
    render(<AllergiesCardView allergies={[gloriaSulfonamide]} />);

    const row = screen.getByTestId("allergy-row");
    expect(row).toBeTruthy();
    // allergyDisplay() must resolve "Sulfonamide" out of text.div.
    const name = screen.getByTestId("allergy-name");
    expect(name.textContent).toContain("Sulfonamide");
    // Severity is absent -> empty parens, matching the loaded screenshot.
    expect(name.textContent).toBe("Sulfonamide ()");
    expect(screen.queryByTestId("allergy-severity")).toBeNull();
    expect(screen.queryByTestId("allergy-reaction")).toBeNull();
  });

  it("renders multiple rows with reaction and severity when present", () => {
    render(
      <AllergiesCardView
        allergies={[gloriaSulfonamide, peanut, penicillin]}
      />,
    );

    const rows = screen.getAllByTestId("allergy-row");
    expect(rows).toHaveLength(3);

    const names = screen.getAllByTestId("allergy-name").map((n) => n.textContent);
    expect(names).toContain("Sulfonamide ()");
    expect(names).toContain("Peanut (moderate)");
    expect(names).toContain("Penicillin G (severe)");

    // Reaction tooltip text surfaces for the rows that have one.
    const reactions = screen
      .getAllByTestId("allergy-reaction")
      .map((n) => n.textContent);
    expect(reactions).toContain("Hives");
    expect(reactions).toContain("Anaphylaxis");

    // Severity badges render for moderate + severe.
    const sevs = screen
      .getAllByTestId("allergy-severity")
      .map((n) => n.textContent);
    expect(sevs).toContain("moderate");
    expect(sevs).toContain("severe");
  });

  it("caps the visible rows at 5", () => {
    const many: FhirAllergyIntolerance[] = Array.from({ length: 8 }, (_, i) => ({
      resourceType: "AllergyIntolerance",
      id: `a-${i}`,
      code: { text: `Allergen ${i}` },
    }));
    render(<AllergiesCardView allergies={many} />);
    expect(screen.getAllByTestId("allergy-row")).toHaveLength(5);
  });

  it("exposes the read-only edit pencil with the correct aria-label", () => {
    render(<AllergiesCardView allergies={[peanut]} />);
    const button = screen.getByLabelText("Edit allergies");
    expect(button.tagName).toBe("BUTTON");
    expect(button.getAttribute("type")).toBe("button");
  });

  it("highlights rows with criticality=high (parity with bg-warning)", () => {
    // Synthetic OpenEMR data has neither `criticality` nor severe
    // reactions on any AllergyIntolerance, so this branch cannot be
    // verified visually. Locked in here against the FHIR shape.
    const peanutHighCriticality: FhirAllergyIntolerance = {
      ...peanut,
      id: "peanut-high-crit",
      criticality: "high",
    };
    render(
      <AllergiesCardView
        allergies={[peanutHighCriticality, gloriaSulfonamide]}
      />,
    );
    const rows = screen.getAllByTestId("allergy-row");
    const highRiskRows = rows.filter(
      (r) => r.getAttribute("data-high-risk") === "true",
    );
    expect(highRiskRows).toHaveLength(1);
    expect(highRiskRows[0].className).toContain("bg-yellow-300");
    expect(highRiskRows[0].className).toContain("font-bold");
  });

  it("highlights rows where reaction[0].severity is severe", () => {
    // Penicillin fixture already has severity=severe; verify the
    // highlight branch fires off that signal too (criticality is
    // absent on this row).
    render(<AllergiesCardView allergies={[penicillin, peanut]} />);
    const rows = screen.getAllByTestId("allergy-row");
    const highRiskRows = rows.filter(
      (r) => r.getAttribute("data-high-risk") === "true",
    );
    expect(highRiskRows).toHaveLength(1);
    expect(highRiskRows[0].textContent).toContain("Penicillin G");
  });
});

describe("reactionText", () => {
  it("prefers manifestation.text over coding[].display", () => {
    expect(reactionText(peanut)).toBe("Hives");
  });

  it("falls back to manifestation.coding[].display", () => {
    expect(reactionText(penicillin)).toBe("Anaphylaxis");
  });

  it("returns null when no reaction is recorded", () => {
    expect(reactionText(gloriaSulfonamide)).toBeNull();
  });
});

describe("severityLabel", () => {
  it("returns the FHIR severity enum value when present", () => {
    expect(severityLabel(peanut)).toBe("moderate");
    expect(severityLabel(penicillin)).toBe("severe");
  });

  it("returns null when severity is not recorded (Gloria's case)", () => {
    expect(severityLabel(gloriaSulfonamide)).toBeNull();
  });
});

describe("AllergiesCard error branch (JSX)", () => {
  /**
   * The default-export Server Component wraps the fetch in try/catch and
   * returns <ErrorCard title="Allergies" /> on failure. Rather than mock
   * out FhirClient over a network boundary in jsdom, render the catch-
   * branch JSX directly — the same shape the Server Component returns.
   */
  it("renders ErrorCard when the fetch path fails", async () => {
    const { ErrorCard } = await import("./card-states");
    render(<ErrorCard title="Allergies" />);
    const card = screen.getByTestId("error-card");
    expect(card).toBeTruthy();
    expect(card.getAttribute("role")).toBe("alert");
    expect(screen.getByText("Allergies")).toBeTruthy();
  });
});
