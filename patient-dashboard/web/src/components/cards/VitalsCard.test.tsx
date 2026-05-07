// @vitest-environment jsdom
/**
 * VitalsCard render + helper tests.
 *
 * Per-file `@vitest-environment jsdom` matches the other card test
 * files and keeps the rest of the suite running under `node`.
 *
 * The cardinal rule under test is "most-recent of each LOINC". Each
 * fixture below is shaped against the 1.5-verified bundle for Gloria
 * Tran (15 vital-signs Observations, including the panel LOINC
 * `85353-1`, heart rate `8867-4`, and respiratory rate `9279-1`).
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import {
  VitalsCardView,
  displayLabel,
  formatObservationValue,
  lastUpdatedLabel,
  loincOf,
  mostRecentByLoinc,
} from "./VitalsCard";
import type { FhirObservation } from "@/lib/fhir/types";

const heartRate96: FhirObservation = {
  resourceType: "Observation",
  id: "hr-96",
  status: "final",
  code: {
    coding: [
      {
        system: "http://loinc.org",
        code: "8867-4",
        display: "Heart rate",
      },
    ],
  },
  valueQuantity: { value: 96, unit: "/min" },
  effectiveDateTime: "2026-04-29T05:45:00Z",
};

const heartRate72Older: FhirObservation = {
  resourceType: "Observation",
  id: "hr-72-older",
  status: "final",
  code: {
    coding: [{ system: "http://loinc.org", code: "8867-4", display: "Heart rate" }],
  },
  valueQuantity: { value: 72, unit: "/min" },
  effectiveDateTime: "2025-12-01T09:00:00Z",
};

const respiratoryRate18: FhirObservation = {
  resourceType: "Observation",
  id: "rr-18",
  status: "final",
  code: {
    coding: [
      {
        system: "http://loinc.org",
        code: "9279-1",
        display: "Respiratory rate",
      },
    ],
  },
  valueQuantity: { value: 18, unit: "/min" },
  effectiveDateTime: "2026-04-29T05:45:00Z",
};

/**
 * Vital-signs panel container — the row whose LOINC is `85353-1`. It
 * carries no top-level value and no useful component data on its own;
 * its leaf observations come back as their own top-level rows. The
 * filter MUST drop this entry from the rendered list.
 */
const vitalSignsPanel: FhirObservation = {
  resourceType: "Observation",
  id: "panel-1",
  status: "final",
  code: {
    coding: [
      {
        system: "http://loinc.org",
        code: "85353-1",
        display: "Vital signs panel",
      },
    ],
  },
  effectiveDateTime: "2026-04-29T05:45:00Z",
};

/** A row whose only "value" is in `component[]` and which lacks the
 *  conventional `valueQuantity`. Used to verify the card renders
 *  gracefully (no crash; falls back to the placeholder) when given
 *  a shape it does not specifically know how to format. */
const componentOnlyMystery: FhirObservation = {
  resourceType: "Observation",
  id: "mystery-1",
  status: "final",
  code: {
    coding: [
      {
        system: "http://loinc.org",
        code: "99999-0",
        display: "Mystery vital",
      },
    ],
  },
  component: [
    {
      code: { coding: [{ code: "11111-1" }] },
      valueQuantity: { value: 42, unit: "x" },
    },
  ],
  effectiveDateTime: "2026-04-29T05:45:00Z",
};

/** A row whose value is a free-text valueString (no quantity). Verifies
 *  the formatter falls back to the string and the card renders without
 *  error. */
const stringOnly: FhirObservation = {
  resourceType: "Observation",
  id: "string-1",
  status: "final",
  code: {
    coding: [
      {
        system: "http://loinc.org",
        code: "88888-2",
        display: "Free-text vital",
      },
    ],
  },
  valueString: "Within normal limits",
  effectiveDateTime: "2026-04-29T05:45:00Z",
};

describe("VitalsCardView (render)", () => {
  it("renders the EmptyCard with 'No vitals recorded' for an empty bundle", () => {
    render(<VitalsCardView observations={[]} />);
    expect(screen.getByTestId("empty-card")).toBeTruthy();
    expect(screen.getByText("No vitals recorded")).toBeTruthy();
    expect(screen.queryByTestId("vitals-list")).toBeNull();
  });

  it("renders heart rate and respiratory rate while filtering out the vital-signs panel", () => {
    render(
      <VitalsCardView
        observations={[heartRate96, respiratoryRate18, vitalSignsPanel]}
      />,
    );

    const labels = screen.getAllByTestId("vital-label").map((n) => n.textContent);
    const values = screen.getAllByTestId("vital-value").map((n) => n.textContent);

    // Two rows survive — the panel LOINC 85353-1 must NOT appear.
    expect(labels).toEqual(expect.arrayContaining(["Heart rate", "Respiratory rate"]));
    expect(labels).not.toContain("Vital signs panel");
    expect(values).toEqual(expect.arrayContaining(["96 /min", "18 /min"]));

    // "Last updated" subtitle is present and formatted YYYY-MM-DD HH:mm.
    const subtitle = screen.getByTestId("vitals-last-updated");
    expect(subtitle.textContent).toBe("Last updated: 2026-04-29 05:45");
  });

  it("renders ONLY the most-recent observation per LOINC (the rule under test)", () => {
    // Two heart-rate readings on different dates plus a panel.
    render(
      <VitalsCardView
        observations={[heartRate72Older, heartRate96, vitalSignsPanel]}
      />,
    );

    const rows = screen.getAllByTestId("vital-label");
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toBe("Heart rate");

    const value = screen.getByTestId("vital-value");
    // 96/min wins (effectiveDateTime 2026-04-29 > 2025-12-01).
    expect(value.textContent).toBe("96 /min");
  });

  it("renders gracefully when an observation has no valueQuantity", () => {
    // componentOnlyMystery: component[] but no top-level valueQuantity.
    // stringOnly: valueString only. Neither should crash; the formatter
    // surfaces "—" for the mystery row and the string for stringOnly.
    expect(() =>
      render(
        <VitalsCardView observations={[componentOnlyMystery, stringOnly]} />,
      ),
    ).not.toThrow();

    const values = screen.getAllByTestId("vital-value").map((n) => n.textContent);
    expect(values).toContain("Within normal limits");
    expect(values).toContain("—"); // em-dash placeholder
  });

  it("exposes the read-only edit pencil with the correct aria-label", () => {
    render(<VitalsCardView observations={[heartRate96]} />);
    const button = screen.getByLabelText("Edit vitals");
    expect(button.tagName).toBe("BUTTON");
    expect(button.getAttribute("type")).toBe("button");
  });
});

describe("loincOf", () => {
  it("returns the first coding's code", () => {
    expect(loincOf(heartRate96)).toBe("8867-4");
  });

  it("returns null when no coding is present", () => {
    expect(
      loincOf({
        resourceType: "Observation",
        id: "x",
        status: "final",
        code: {},
      }),
    ).toBeNull();
  });
});

describe("mostRecentByLoinc", () => {
  it("drops the vital-signs panel LOINC 85353-1 entirely", () => {
    const out = mostRecentByLoinc([vitalSignsPanel, heartRate96]);
    const codes = out.map(loincOf);
    expect(codes).not.toContain("85353-1");
    expect(codes).toContain("8867-4");
  });

  it("keeps the latest entry per LOINC group", () => {
    const out = mostRecentByLoinc([heartRate72Older, heartRate96]);
    expect(out).toHaveLength(1);
    expect(out[0].id).toBe("hr-96");
  });
});

describe("formatObservationValue", () => {
  it("renders 'value unit' for a standard valueQuantity", () => {
    expect(formatObservationValue(heartRate96)).toBe("96 /min");
  });

  it("falls back to valueString when valueQuantity is absent", () => {
    expect(formatObservationValue(stringOnly)).toBe("Within normal limits");
  });

  it("returns null for a row with no recognisable value path", () => {
    expect(formatObservationValue(componentOnlyMystery)).toBeNull();
  });

  it("renders BP panel as systolic/diastolic from components", () => {
    const bpPanel: FhirObservation = {
      resourceType: "Observation",
      id: "bp-1",
      status: "final",
      code: {
        coding: [{ system: "http://loinc.org", code: "85354-9", display: "Blood pressure" }],
      },
      effectiveDateTime: "2026-04-29T05:45:00Z",
      component: [
        {
          code: { coding: [{ code: "8480-6", display: "Systolic" }] },
          valueQuantity: { value: 156, unit: "mmHg" },
        },
        {
          code: { coding: [{ code: "8462-4", display: "Diastolic" }] },
          valueQuantity: { value: 94, unit: "mmHg" },
        },
      ],
    };
    expect(formatObservationValue(bpPanel)).toBe("156/94 mmHg");
  });
});

describe("displayLabel", () => {
  it("prefers code.coding[0].display", () => {
    expect(displayLabel(heartRate96)).toBe("Heart rate");
  });

  it("falls back to code.text", () => {
    const obs: FhirObservation = {
      resourceType: "Observation",
      id: "y",
      status: "final",
      code: { text: "Custom" },
    };
    expect(displayLabel(obs)).toBe("Custom");
  });
});

describe("lastUpdatedLabel", () => {
  it("formats the latest effectiveDateTime as YYYY-MM-DD HH:mm", () => {
    expect(lastUpdatedLabel([heartRate72Older, heartRate96])).toBe(
      "2026-04-29 05:45",
    );
  });

  it("returns null when no observation has an effectiveDateTime", () => {
    const obs: FhirObservation = {
      resourceType: "Observation",
      id: "z",
      status: "final",
      code: { coding: [{ code: "1" }] },
    };
    expect(lastUpdatedLabel([obs])).toBeNull();
  });
});

describe("VitalsCard error branch (JSX)", () => {
  /**
   * The default-export Server Component wraps the fetch in try/catch
   * and returns <ErrorCard title="Vitals" /> on failure. Render the
   * catch-branch JSX directly — same shape the Server Component
   * returns — rather than mock FhirClient over a network boundary in
   * jsdom.
   */
  it("renders ErrorCard when the fetch path fails", async () => {
    const { ErrorCard } = await import("./card-states");
    render(<ErrorCard title="Vitals" />);
    const card = screen.getByTestId("error-card");
    expect(card).toBeTruthy();
    expect(card.getAttribute("role")).toBe("alert");
    expect(screen.getByText("Vitals")).toBeTruthy();
  });
});
