// @vitest-environment jsdom
/**
 * MedicationsCard render + integration-with-synthesis tests.
 *
 * These tests exercise the *view* component plus the shared
 * `filterMedications` helper from `@/lib/fhir/synthesis` — the same
 * helper the Server Component uses. We deliberately do NOT re-implement
 * filtering here; we feed the helper's output to the view so the test
 * surface matches production.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import {
  MedicationsCardView,
  formatMedicationDisplay,
} from "./MedicationsCard";
import { filterMedications } from "@/lib/fhir/synthesis";
import type { FhirMedicationRequest } from "@/lib/fhir/types";

// Gloria Tran's two active medications (1.5 verification fixture).
// Both come back as intent=plan with a populated medicationCodeableConcept.
const furosemide: FhirMedicationRequest = {
  resourceType: "MedicationRequest",
  id: "med-furosemide",
  status: "active",
  intent: "plan",
  medicationCodeableConcept: { text: "Furosemide 80mg IV" },
};

const lisinopril: FhirMedicationRequest = {
  resourceType: "MedicationRequest",
  id: "med-lisinopril",
  status: "active",
  intent: "plan",
  medicationCodeableConcept: { text: "Lisinopril 10mg PO" },
};

const stoppedAtorvastatin: FhirMedicationRequest = {
  resourceType: "MedicationRequest",
  id: "med-atorvastatin",
  status: "stopped",
  intent: "plan",
  medicationCodeableConcept: { text: "Atorvastatin 20mg PO" },
};

describe("MedicationsCardView (render)", () => {
  it("renders Gloria's two active medications from the filtered fixture", () => {
    const meds = filterMedications([furosemide, lisinopril]);
    render(<MedicationsCardView medications={meds} />);

    expect(screen.getByTestId("medications-card")).toBeTruthy();
    const rows = screen.getAllByTestId("medication-row");
    expect(rows).toHaveLength(2);
    const text = rows.map((r) => r.textContent);
    expect(text).toEqual(
      expect.arrayContaining([
        expect.stringContaining("Furosemide 80mg IV"),
        expect.stringContaining("Lisinopril 10mg PO"),
      ]),
    );
    // The edit pencil stub is present and labelled.
    expect(screen.getByTestId("medications-edit")).toBeTruthy();
  });

  it("renders the EmptyCard 'None' state when filterMedications returns nothing", () => {
    const meds = filterMedications([]);
    render(<MedicationsCardView medications={meds} />);

    expect(screen.getByTestId("empty-card")).toBeTruthy();
    expect(screen.getByTestId("empty-card").textContent).toContain(
      "Medications",
    );
    expect(screen.getByTestId("empty-card").textContent).toContain("None");
  });

  it("uses filterMedications to drop status=stopped rows before render", () => {
    // Feed mixed list through the real helper and assert the stopped row
    // is gone — proves the card relies on the shared synthesis rule
    // rather than re-implementing it.
    const meds = filterMedications([
      furosemide,
      stoppedAtorvastatin,
      lisinopril,
    ]);
    render(<MedicationsCardView medications={meds} />);

    const rows = screen.getAllByTestId("medication-row");
    expect(rows).toHaveLength(2);
    expect(
      rows.some((r) => r.textContent?.includes("Atorvastatin")),
    ).toBe(false);
  });

  it("appends a non-active status (e.g. on-hold) parenthetically", () => {
    const onHold: FhirMedicationRequest = {
      ...furosemide,
      id: "med-onhold",
      status: "on-hold",
    };
    const meds = filterMedications([onHold]);
    render(<MedicationsCardView medications={meds} />);

    expect(screen.getByTestId("medication-status").textContent).toBe(
      "(on-hold)",
    );
  });
});

describe("formatMedicationDisplay", () => {
  it("prefers medicationCodeableConcept.text", () => {
    expect(formatMedicationDisplay(furosemide)).toBe("Furosemide 80mg IV");
  });

  it("falls back to coding[0].display when text is absent", () => {
    const req: FhirMedicationRequest = {
      resourceType: "MedicationRequest",
      id: "x",
      status: "active",
      intent: "plan",
      medicationCodeableConcept: {
        coding: [{ display: "Metformin 500mg tablet" }],
      },
    };
    expect(formatMedicationDisplay(req)).toBe("Metformin 500mg tablet");
  });

  it("returns 'Unknown medication' when nothing is populated", () => {
    const req: FhirMedicationRequest = {
      resourceType: "MedicationRequest",
      id: "x",
      status: "active",
      intent: "plan",
    };
    expect(formatMedicationDisplay(req)).toBe("Unknown medication");
  });

  it("appends dosageInstruction[0].text when distinct from the name", () => {
    const req: FhirMedicationRequest = {
      resourceType: "MedicationRequest",
      id: "x",
      status: "active",
      intent: "plan",
      medicationCodeableConcept: { text: "Metformin" },
      dosageInstruction: [{ text: "500mg PO BID" }],
    };
    expect(formatMedicationDisplay(req)).toBe("Metformin — 500mg PO BID");
  });
});

describe("MedicationsCard error path", () => {
  it("renders an ErrorCard when the entry-point catches a fetch failure", async () => {
    // Force the entry-point Server Component down its catch branch by
    // pointing OPENEMR_BASE_URL at an unreachable host and stubbing
    // fetch to throw. Importing dynamically so the module reads the
    // env var we just set.
    const prev = process.env.OPENEMR_BASE_URL;
    process.env.OPENEMR_BASE_URL = "http://127.0.0.1:1";
    const originalFetch = globalThis.fetch;
    globalThis.fetch = (() =>
      Promise.reject(new Error("network down"))) as typeof fetch;

    try {
      const mod = await import("./MedicationsCard");
      const element = await mod.default({ patientUuid: "fake-uuid" });
      render(element);
      expect(screen.getByTestId("error-card")).toBeTruthy();
      expect(screen.getByTestId("error-card").textContent).toContain(
        "Medications",
      );
    } finally {
      globalThis.fetch = originalFetch;
      if (prev === undefined) {
        delete process.env.OPENEMR_BASE_URL;
      } else {
        process.env.OPENEMR_BASE_URL = prev;
      }
    }
  });
});
