// @vitest-environment jsdom
/**
 * PrescriptionsCard render tests.
 *
 * Per the 1.5 verification recorded in dashboard-inventory.md, every
 * synthetic patient returns zero MedicationRequests with `intent=order`.
 * The empty "None" state is therefore the realistic default; we cover it
 * first. The loaded path is exercised against a hand-built fixture that
 * matches the synthesis rule (`intent=order` + `status=active` + requester
 * populated), and a mixed fixture verifies that `filterPrescriptions`
 * actually drops `intent=plan` rows.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import {
  PrescriptionsCardView,
  formatMedicationName,
} from "./PrescriptionsCard";
import { ErrorCard } from "./card-states";
import { filterPrescriptions } from "@/lib/fhir/synthesis";
import type { FhirMedicationRequest } from "@/lib/fhir/types";

const orderActiveWithRequester: FhirMedicationRequest = {
  resourceType: "MedicationRequest",
  id: "rx-1",
  status: "active",
  intent: "order",
  medicationCodeableConcept: { text: "Lisinopril 10 MG Oral Tablet" },
  dosageInstruction: [{ text: "Take 1 tablet by mouth daily" }],
  requester: { display: "Dr. Alice Provider" },
};

const planNoRequester: FhirMedicationRequest = {
  resourceType: "MedicationRequest",
  id: "rx-plan-1",
  status: "active",
  intent: "plan",
  medicationCodeableConcept: { text: "Patient-reported aspirin" },
};

const anotherPlan: FhirMedicationRequest = {
  resourceType: "MedicationRequest",
  id: "rx-plan-2",
  status: "active",
  intent: "plan",
  medicationCodeableConcept: { text: "Patient-reported vitamin D" },
};

describe("PrescriptionsCardView (render)", () => {
  it("renders 'None' when the filtered list is empty (the synthetic-data default)", () => {
    render(<PrescriptionsCardView prescriptions={[]} />);

    // EmptyCard chrome: title + 'None' message.
    expect(screen.getByTestId("empty-card")).toBeTruthy();
    expect(screen.getByText("Prescriptions")).toBeTruthy();
    expect(screen.getByText("None")).toBeTruthy();
    // The loaded card chrome should not render in the empty state.
    expect(screen.queryByTestId("prescriptions-card")).toBeNull();
    expect(screen.queryByTestId("prescriptions-list")).toBeNull();
  });

  it("renders a row with name, dosage, and prescriber for an order+active+requester entry", () => {
    render(
      <PrescriptionsCardView prescriptions={[orderActiveWithRequester]} />,
    );

    expect(screen.getByTestId("prescriptions-card")).toBeTruthy();
    const rows = screen.getAllByTestId("prescription-row");
    expect(rows).toHaveLength(1);
    expect(screen.getByTestId("prescription-name").textContent).toBe(
      "Lisinopril 10 MG Oral Tablet",
    );
    expect(screen.getByTestId("prescription-dosage").textContent).toBe(
      "Take 1 tablet by mouth daily",
    );
    expect(screen.getByTestId("prescription-prescriber").textContent).toBe(
      "Prescribed by Dr. Alice Provider",
    );
    // Edit pencil renders and is a real button (stubbed handler).
    expect(screen.getByTestId("prescriptions-edit")).toBeTruthy();
  });

  it("filterPrescriptions drops intent=plan, keeps the lone intent=order entry", () => {
    const mixed: FhirMedicationRequest[] = [
      planNoRequester,
      anotherPlan,
      orderActiveWithRequester,
    ];

    const filtered = filterPrescriptions(mixed);
    expect(filtered).toHaveLength(1);
    expect(filtered[0].id).toBe("rx-1");

    render(<PrescriptionsCardView prescriptions={filtered} />);
    const rows = screen.getAllByTestId("prescription-row");
    expect(rows).toHaveLength(1);
    expect(screen.getByTestId("prescription-name").textContent).toBe(
      "Lisinopril 10 MG Oral Tablet",
    );
  });

  it("edit pencil click logs the TODO without throwing", () => {
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    render(
      <PrescriptionsCardView prescriptions={[orderActiveWithRequester]} />,
    );

    screen.getByTestId("prescriptions-edit").click();
    expect(log).toHaveBeenCalledWith(
      "[PrescriptionsCard] TODO: wire edit affordance — read-only this phase",
    );
    log.mockRestore();
  });
});

describe("formatMedicationName", () => {
  it("prefers medicationCodeableConcept.text", () => {
    expect(formatMedicationName(orderActiveWithRequester)).toBe(
      "Lisinopril 10 MG Oral Tablet",
    );
  });

  it("falls back to coding[].display, then medicationReference.display, then 'Unknown medication'", () => {
    expect(
      formatMedicationName({
        resourceType: "MedicationRequest",
        id: "x",
        status: "active",
        intent: "order",
        medicationCodeableConcept: { coding: [{ display: "Coded Drug" }] },
      }),
    ).toBe("Coded Drug");

    expect(
      formatMedicationName({
        resourceType: "MedicationRequest",
        id: "x",
        status: "active",
        intent: "order",
        medicationReference: { display: "Referenced Drug" },
      }),
    ).toBe("Referenced Drug");

    expect(
      formatMedicationName({
        resourceType: "MedicationRequest",
        id: "x",
        status: "active",
        intent: "order",
      }),
    ).toBe("Unknown medication");
  });
});

describe("Error path", () => {
  it("renders ErrorCard with the Prescriptions title when the fetch throws", () => {
    // The Server Component catches errors and returns <ErrorCard title="Prescriptions" />.
    // Render that exact branch directly — the fetch itself is exercised in
    // the FhirClient suite.
    render(<ErrorCard title="Prescriptions" />);
    expect(screen.getByTestId("error-card")).toBeTruthy();
    expect(screen.getByText("Prescriptions")).toBeTruthy();
    expect(
      screen.getByText("Could not load this section. Try again later."),
    ).toBeTruthy();
  });
});
