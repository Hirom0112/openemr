// @vitest-environment jsdom
/**
 * MedicalProblemsCard render tests.
 *
 * Per-file `@vitest-environment jsdom` — keeps the rest of the suite
 * running under `node` (the FhirClient tests in src/lib/fhir/__tests__
 * depend on the node environment).
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import {
  MedicalProblemsCardView,
  filterVisibleProblems,
} from "./MedicalProblemsCard";
import { ErrorCard } from "./card-states";
import type { FhirCondition } from "@/lib/fhir/types";

const heartFailure: FhirCondition = {
  resourceType: "Condition",
  id: "cond-1",
  clinicalStatus: {
    coding: [
      {
        system:
          "http://terminology.hl7.org/CodeSystem/condition-clinical",
        code: "active",
      },
    ],
  },
  code: {
    text: "Heart failure",
    coding: [{ system: "http://snomed.info/sct", code: "84114007", display: "Heart failure" }],
  },
  onsetDateTime: "2018-04-12",
};

const resolvedFlu: FhirCondition = {
  resourceType: "Condition",
  id: "cond-2",
  clinicalStatus: {
    coding: [
      {
        system:
          "http://terminology.hl7.org/CodeSystem/condition-clinical",
        code: "resolved",
      },
    ],
  },
  code: { text: "Influenza" },
  onsetDateTime: "2024-01-15",
};

describe("MedicalProblemsCardView (render)", () => {
  it("renders 'None' empty state when there are no visible problems", () => {
    render(<MedicalProblemsCardView conditions={[]} />);
    const empty = screen.getByTestId("empty-card");
    expect(empty.textContent).toContain("Medical Problems");
    expect(empty.textContent).toContain("None");
  });

  it("renders a row for an active problem with onset date and status badge", () => {
    render(<MedicalProblemsCardView conditions={[heartFailure]} />);

    const rows = screen.getAllByTestId("problem-row");
    expect(rows).toHaveLength(1);

    expect(screen.getByTestId("problem-display").textContent).toBe(
      "Heart failure",
    );
    expect(screen.getByTestId("problem-onset").textContent).toBe(
      "2018-04-12",
    );
    const badge = screen.getByTestId("problem-status");
    expect(badge.textContent).toBe("active");
    expect(badge.getAttribute("data-status")).toBe("active");
  });

  it("filters resolved conditions out of the visible list", () => {
    render(
      <MedicalProblemsCardView
        conditions={[heartFailure, resolvedFlu]}
      />,
    );

    const rows = screen.getAllByTestId("problem-row");
    expect(rows).toHaveLength(1);
    // Influenza was resolved -> excluded; Heart failure remains.
    expect(screen.queryByText("Influenza")).toBeNull();
    expect(screen.getByTestId("problem-display").textContent).toBe(
      "Heart failure",
    );
  });

  it("collapses to the 'None' empty state when every condition is resolved", () => {
    render(<MedicalProblemsCardView conditions={[resolvedFlu]} />);
    const empty = screen.getByTestId("empty-card");
    expect(empty.textContent).toContain("None");
    expect(screen.queryByTestId("problem-row")).toBeNull();
  });

  it("renders an ErrorCard for the error state (parity with PatientHeader)", () => {
    // The Server Component wraps fetch failures in ErrorCard; we render
    // the card-state primitive directly to assert the user-facing shape.
    render(<ErrorCard title="Medical Problems" />);
    const err = screen.getByTestId("error-card");
    expect(err.textContent).toContain("Medical Problems");
  });
});

describe("filterVisibleProblems", () => {
  it("keeps active and inactive, drops resolved", () => {
    const inactive: FhirCondition = {
      resourceType: "Condition",
      id: "c-3",
      clinicalStatus: { coding: [{ code: "inactive" }] },
      code: { text: "Old issue" },
    };
    const result = filterVisibleProblems([
      heartFailure,
      resolvedFlu,
      inactive,
    ]);
    expect(result.map((c) => c.id)).toEqual(["cond-1", "c-3"]);
  });

  it("treats missing clinicalStatus as visible (synthetic-data fallback)", () => {
    const noStatus: FhirCondition = {
      resourceType: "Condition",
      id: "c-4",
      code: { text: "Unknown-status problem" },
    };
    expect(filterVisibleProblems([noStatus]).map((c) => c.id)).toEqual([
      "c-4",
    ]);
  });
});
